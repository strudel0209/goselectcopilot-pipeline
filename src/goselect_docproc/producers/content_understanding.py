"""Content Understanding producer — Azure-native classify, split and route.

``contentCategories`` classifies **and splits** a multi-document package in one
call, with categories defined by description rather than training data. That
removes the labelling burden the DI custom-classifier path carries.

**The limitation that shapes the design.** Content Understanding's minimum unit
is one page:

    "The minimum unit for classification of documents is a single page.
     Intra-page classification isn't supported."

So this producer emits **whole-page regions** and cannot separate a schedule
from an inset diagram on the same sheet. On the ABB sample, **15 of 20 pages
carry more than one content kind and 4 carry all three** — so on that corpus this
producer structurally cannot match the DI path for intra-page
routing. Whether that matters is precisely what the bench-off measures.

Mitigation, and the reason this producer is still a serious contender: run it as
the **router** and keep DI Layout for intra-page geometry.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Protocol, runtime_checkable

from ..contracts import ContentType, Region, SectionNode, Segment, Span
from ..sections import SectionIndex
from ..spans import subtract
from .base import DocumentAnalysis, ProducerCapabilities, ProducerCost, register

log = logging.getLogger(__name__)

FURNITURE_ROLES = {"pageHeader", "pageFooter", "pageNumber"}

API_VERSION = "2025-11-01"
USD_PER_PAGE = 0.010

# The service rejects '-' in an analyzer id, so keep this alphanumeric.
DEFAULT_ANALYZER_ID = "goselectRouter"

CATEGORY_TO_KIND: dict[str, ContentType] = {
    "TextSpecification": ContentType.TEXT,
    "EquipmentSchedule": ContentType.SCHEDULE,
    "Drawing": ContentType.DRAWING,
    "Other": ContentType.OTHER,
}

ROUTER_ANALYZER: dict[str, Any] = {
    "baseAnalyzerId": "prebuilt-document",
    "description": "GoSelect Copilot router: classify and split a specification package",
    "config": {
        "returnDetails": True,
        "enableSegment": True,
        # Must be declared false: the service default is true, and on a drawing the
        # box around a tag is read as a radical sign - VFD-401 becomes \sqrt{150-401}.
        "enableFormula": False,
        # Add "analyzerId" to a category to route it to a purpose-built analyzer.
        "contentCategories": {
            "TextSpecification": {
                "description": (
                    "Narrative technical specification prose: numbered sections and "
                    "sub-sections, requirement clauses, scope of supply, standards "
                    "references. Predominantly paragraphs, few or no tables, no drawing "
                    "border or title block."
                )
            },
            "EquipmentSchedule": {
                "description": (
                    "Tabular equipment, motor, VFD or panel schedule. Page is dominated "
                    "by one or more grids with column headers such as Tag, HP, kW, "
                    "Voltage, FLA, Enclosure, Service."
                )
            },
            "Drawing": {
                "description": (
                    "Engineering drawing sheet: single-line diagram, P&ID, layout or "
                    "elevation. Has a drawing border with a zone grid, a title block in "
                    "the lower right, symbols connected by lines, and sparse rotated text."
                )
            },
            # Without a catch-all, content is forced into one of the three above.
            "Other": {
                "description": "Cover pages, transmittals, blank pages, revision histories."
            },
        },
    },
    # gpt-4.1 retires October 2026. Measured equal to the flagship on specification
    # prose, so the mini is the default here too.
    "models": {"completion": "gpt-5.4-mini"},
}


@runtime_checkable
class ContentUnderstandingClient(Protocol):
    """Seam over the analyze call so mapping is testable without a key."""

    def analyze(self, *, analyzer_id: str, data: bytes, source_uri: str | None = None) -> Any: ...


@register("content-understanding")
class ContentUnderstandingProducer:
    capabilities = ProducerCapabilities(
        native_regions=True,
        native_figure_crops=False,
        intra_page=True,  # classifier is page-level, but the layout model is not
        residency="azure-native",
    )

    def __init__(
        self,
        client: ContentUnderstandingClient,
        analyzer_id: str = DEFAULT_ANALYZER_ID,
        usd_per_page: float = USD_PER_PAGE,
    ) -> None:
        self.client = client
        self.analyzer_id = analyzer_id
        self.usd_per_page = usd_per_page

    def analyze(self, file_id: str, data: bytes, source_uri: str | None = None) -> DocumentAnalysis:
        response = self.client.analyze(
            analyzer_id=self.analyzer_id, data=data, source_uri=source_uri
        )
        item = _first_content(response)
        if item is None:
            raise RuntimeError("content understanding returned no contents")

        content = _get(item, "markdown") or ""
        pages = _get(item, "pages") or []
        page_of = _page_lookup(pages)

        claims = _classify_elements(item, page_of)
        segments: list[Segment] = []

        for index, block in enumerate(_get(item, "segments") or [], start=1):
            span = _get(block, "span") or {}
            offset = int(_get(span, "offset") or 0)
            length = int(_get(span, "length") or 0)
            first = int(_get(block, "startPageNumber") or 1)
            last = int(_get(block, "endPageNumber") or first)
            category = _get(block, "category") or "Other"
            content_type = CATEGORY_TO_KIND.get(category, ContentType.OTHER)

            segments.append(
                Segment(
                    segment_id=f"{file_id}-seg-{index:03d}",
                    file_id=file_id,
                    first_page=first,
                    last_page=last,
                    content_type=content_type,
                    confidence=round(float(_get(block, "confidence") or 1.0), 3),
                    page_confidences=[1.0] * (last - first + 1),
                    regions=_regions_for(claims, offset, offset + length, content_type),
                    producer=self.name,
                )
            )

        index_ = _section_index_from(item, content)
        for segment in segments:
            segment.section_root = index_.root_for(
                segment.start, inherits=segment.content_type is not ContentType.DRAWING
            )

        page_count = len(pages) or max((s.last_page for s in segments), default=0)
        return DocumentAnalysis(
            file_id=file_id,
            content=content,
            page_count=page_count,
            content_sha256=hashlib.sha256(data).hexdigest(),
            segments=segments,
            section_index=index_,
            producer=self.name,
            cost=ProducerCost(
                pages=page_count,
                api_calls=1,
                usd_estimate=round(page_count * self.usd_per_page, 6),
            ),
            furniture_spans=[Span(offset=o, length=l) for o, l, _, _ in claims if _ is None],
            native=item,
            warnings=[
                "classifier granularity is one page; intra-page regions come from "
                "the layout model, not the classifier"
            ],
        )

    def figure_image(self, analysis: DocumentAnalysis, figure_id: str) -> bytes | None:
        return None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Content Understanding responses arrive as dicts or namespaces."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _first_content(response: Any) -> Any:
    contents = _get(response, "contents")
    if contents:
        return contents[0]
    return response if _get(response, "markdown") else None


def _page_lookup(pages: list[Any]) -> list[tuple[int, int, int]]:
    """``(start, end, pageNumber)`` so an element span resolves to a page."""
    out: list[tuple[int, int, int]] = []
    for page in pages:
        number = int(_get(page, "pageNumber") or 0)
        for span in _get(page, "spans") or []:
            offset = int(_get(span, "offset") or 0)
            out.append((offset, offset + int(_get(span, "length") or 0), number))
    return sorted(out)


def _page_for(offset: int, lookup: list[tuple[int, int, int]]) -> int:
    for start, end, number in lookup:
        if start <= offset < end:
            return number
    return lookup[0][2] if lookup else 1


def _span_of(element: Any) -> tuple[int, int] | None:
    """Layout elements carry ``span`` (singular); segments do too."""
    span = _get(element, "span")
    if span is None:
        spans = _get(element, "spans") or []
        span = spans[0] if spans else None
    if span is None:
        return None
    return int(_get(span, "offset") or 0), int(_get(span, "length") or 0)


def _classify_elements(
    item: Any, page_of: list[tuple[int, int, int]]
) -> list[tuple[int, int, int, ContentType | None]]:
    """Tables and figures claim first; unclaimed paragraphs are narrative.

    ``None`` as the kind marks page furniture, which is dropped by design and
    declared so the coverage proof does not count it as loss.
    """
    claimed: list[tuple[int, int]] = []
    out: list[tuple[int, int, int, ContentType | None]] = []

    for element, kind in (
        *(( t, ContentType.SCHEDULE) for t in (_get(item, "tables") or [])),
        *((f, ContentType.DRAWING) for f in (_get(item, "figures") or [])),
    ):
        span = _span_of(element)
        if not span or not span[1]:
            continue
        kept = subtract([span], claimed)
        for offset, length in kept:
            claimed.append((offset, length))
            out.append((offset, length, _page_for(offset, page_of), kind))

    for paragraph in _get(item, "paragraphs") or []:
        span = _span_of(paragraph)
        if not span or not span[1]:
            continue
        role = _get(paragraph, "role")
        if role in FURNITURE_ROLES:
            claimed.append(span)
            out.append((span[0], span[1], _page_for(span[0], page_of), None))
            continue
        for offset, length in subtract([span], claimed):
            claimed.append((offset, length))
            out.append((offset, length, _page_for(offset, page_of), ContentType.TEXT))

    return sorted(out)


def _regions_for(
    claims: list[tuple[int, int, int, ContentType | None]],
    start: int,
    end: int,
    segment_type: ContentType,
) -> list[Region]:
    """Group this segment's claims into one region per (page, kind)."""
    grouped: dict[tuple[int, ContentType], list[tuple[int, int]]] = {}
    for offset, length, page, kind in claims:
        if kind is None or offset < start or offset >= end:
            continue
        grouped.setdefault((page, kind), []).append((offset, length))

    regions = [
        Region(
            kind=kind,
            ref=f"p{page}:{kind.value.lower()}",
            page=page,
            spans=[Span(offset=o, length=l) for o, l in sorted(spans)],
        )
        for (page, kind), spans in grouped.items()
    ]
    return sorted(regions, key=lambda r: r.start)


def _section_index_from(item: Any, content: str) -> SectionIndex:
    """``sections`` is a native outline; fall back to heading-role paragraphs."""
    nodes: list[SectionNode] = []
    page_of = _page_lookup(_get(item, "pages") or [])

    for paragraph in _get(item, "paragraphs") or []:
        if _get(paragraph, "role") not in ("title", "sectionHeading"):
            continue
        span = _span_of(paragraph)
        text = (_get(paragraph, "content") or "").strip()
        if not span or not text:
            continue
        nodes.append(
            SectionNode(
                offset=span[0],
                heading=text.lstrip("#").strip(),
                page=_page_for(span[0], page_of),
                level=1,
            )
        )

    return SectionIndex(
        nodes=sorted(nodes, key=lambda n: n.offset),
        strategy="content-understanding-roles" if nodes else "content-understanding-none",
        role_headings=len(nodes),
    )


class AzureContentUnderstandingClient:
    """Minimal REST client. Stdlib only, so it adds no dependency.

    Auth precedence: subscription key when supplied, otherwise Entra ID via
    ``DefaultAzureCredential`` (which needs ``Cognitive Services User`` on the
    Foundry resource).
    """

    SCOPE = "https://cognitiveservices.azure.com/.default"

    def __init__(
        self,
        endpoint: str,
        api_version: str = API_VERSION,
        credential: Any = None,
        api_key: str | None = None,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_version = api_version
        self.api_key = api_key or None
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

        if self.api_key:
            self.credential = None
        else:
            from azure.identity import DefaultAzureCredential

            self.credential = credential or DefaultAzureCredential()

    def _headers(self, content_type: str) -> dict[str, str]:
        if self.api_key:
            return {"Ocp-Apim-Subscription-Key": self.api_key, "Content-Type": content_type}
        token = self.credential.get_token(self.SCOPE).token
        return {"Authorization": f"Bearer {token}", "Content-Type": content_type}

    def _request(self, method: str, url: str, body: bytes | None, content_type: str) -> Any:
        import json as _json
        import urllib.request

        request = urllib.request.Request(
            url, data=body, method=method, headers=self._headers(content_type)
        )
        with urllib.request.urlopen(request) as response:
            payload = response.read()
            location = response.headers.get("Operation-Location")
            parsed = _json.loads(payload) if payload else {}
            return parsed, location

    def ensure_analyzer(self, analyzer_id: str, definition: dict[str, Any] | None = None) -> None:
        """Create or update the analyzer. Creation is **asynchronous**: the PUT
        returns 201 with an Operation-Location that must be polled, so this
        blocks until the analyzer is actually usable.
        """
        import json as _json
        import urllib.error

        url = f"{self.endpoint}/contentunderstanding/analyzers/{analyzer_id}?api-version={self.api_version}"
        body = _json.dumps(definition or ROUTER_ANALYZER).encode()
        try:
            _, operation = self._request("PUT", url, body, "application/json")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise RuntimeError(f"analyzer PUT failed ({exc.code}): {detail}") from exc

        if operation:
            self._poll(operation, what=f"analyzer {analyzer_id} creation")
        log.info("analyzer %s ready", analyzer_id)

    def _poll(self, operation_url: str, what: str) -> dict[str, Any]:
        import time

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            payload, _ = self._request("GET", operation_url, None, "application/json")
            status = str(payload.get("status", "")).lower()
            if status in {"succeeded", "ready"}:
                return payload
            if status in {"failed", "canceled"}:
                raise RuntimeError(f"{what} {status}: {payload.get('error')}")
            time.sleep(self.poll_seconds)
        raise TimeoutError(f"{what} did not complete within {self.timeout_seconds}s")

    def analyze(self, *, analyzer_id: str, data: bytes, source_uri: str | None = None) -> Any:
        """The documented request takes a **URL**, not raw bytes.

        For local testing either upload the file to Blob and pass a SAS URL, or
        rely on the undocumented binary endpoint used as a fallback below.
        """
        import json as _json
        import urllib.error

        base = f"{self.endpoint}/contentunderstanding/analyzers/{analyzer_id}"
        version = f"?api-version={self.api_version}"

        if source_uri and source_uri.startswith(("http://", "https://")):
            body = _json.dumps({"inputs": [{"url": source_uri}]}).encode()
            _, operation = self._request("POST", f"{base}:analyze{version}", body, "application/json")
        else:
            try:
                _, operation = self._request(
                    "POST", f"{base}:analyzeBinary{version}", data, "application/octet-stream"
                )
            except urllib.error.HTTPError as exc:
                raise RuntimeError(
                    "Content Understanding analyze needs an http(s) URL. Upload the "
                    "document to Blob Storage and pass a SAS URL as source_uri "
                    f"(binary fallback returned {exc.code})."
                ) from exc

        if not operation:
            raise RuntimeError("no Operation-Location returned by analyze")
        result = self._poll(operation, what="analyze")
        return _as_namespace(result.get("result") or result)


def _as_namespace(value: Any) -> Any:
    from types import SimpleNamespace

    if isinstance(value, dict):
        return SimpleNamespace(**{k: _as_namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_as_namespace(v) for v in value]
    return value
