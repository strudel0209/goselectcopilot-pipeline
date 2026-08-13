"""Mistral OCR producer — block labels instead of derived geometry.

``include_blocks=True`` returns paragraph-level bounding boxes already labelled
(``text``, ``title``, ``list``, ``table``, ``image``, ``equation``, ``caption``,
``code``, ``references``, ``aside_text``, ``header``, ``footer``, ``signature``).
Where that labelling is accurate it replaces most of ``segmentation.py`` and
``regions.py`` — the service does the classification, and the spine keeps doing
what it is good at.

**Constraints that decide whether this is viable for ABB.** On Azure
(``mistral-ocr-4-0``, Preview, sold directly by Azure):

* **30 pages / 30 MB per request.** The customer's own document table says
  textual specifications run **1–40 pp**, so chunking is mandatory. This module
  chunks by page range and **rebases offsets**, because per-request offsets
  restart at zero and the spine's ordering key would otherwise be meaningless.
* **English only.** The first-party Mistral API advertises 170 languages; the
  Azure-hosted variant does not. For a global customer base that is a hard
  filter, not a preference.
* **Preview.** Not committable for production yet.

All three are surfaced as ``warnings`` on the analysis rather than being
discovered in production.
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

PAGE_SEPARATOR = "\n\n<!-- PageBreak -->\n\n"

BLOCK_TO_KIND: dict[str, ContentType] = {
    "table": ContentType.SCHEDULE,
    "image": ContentType.DRAWING,
    "figure": ContentType.DRAWING,
    "text": ContentType.TEXT,
    "title": ContentType.TEXT,
    "list": ContentType.TEXT,
    "caption": ContentType.TEXT,
    "code": ContentType.TEXT,
    "equation": ContentType.TEXT,
    "references": ContentType.TEXT,
    "aside_text": ContentType.TEXT,
}
FURNITURE_BLOCKS = {"header", "footer", "page_number", "signature"}
HEADING_BLOCKS = {"title"}

# Direct-API list price. Azure sold-direct pricing differs; override to compare.
USD_PER_PAGE = 0.004


@runtime_checkable
class MistralOCRClient(Protocol):
    """Seam over ``ocr.process`` so mapping is testable without a key."""

    def process(self, *, document: dict[str, Any], pages: list[int] | None, **kwargs: Any) -> Any: ...


class MistralOCRRestClient:
    """Stdlib REST client for ``POST /v1/ocr``.

    Used in preference to the official SDK because ``include_blocks`` — the
    whole reason this producer exists — only appears in OCR 4 (June 2026), and
    older packaged SDK builds silently lack the parameter. Talking to the REST
    surface directly keeps the request explicit and adds no dependency.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.mistral.ai",
        path: str = "/v1/ocr",
        timeout_seconds: float = 300.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.timeout_seconds = timeout_seconds

    def process(self, **kwargs: Any) -> Any:
        import json as _json
        import urllib.error
        import urllib.request

        body = {k: v for k, v in kwargs.items() if v is not None}
        request = urllib.request.Request(
            f"{self.base_url}{self.path}",
            data=_json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = _json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise RuntimeError(f"mistral ocr failed ({exc.code}): {detail}") from exc
        return _as_namespace(payload)


def _as_namespace(value: Any) -> Any:
    from types import SimpleNamespace

    if isinstance(value, dict):
        return SimpleNamespace(**{k: _as_namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_as_namespace(v) for v in value]
    return value


@register("mistral-blocks")
class MistralBlocksProducer:
    capabilities = ProducerCapabilities(
        max_pages=30,
        max_bytes=30 * 1024 * 1024,
        languages=("en",),
        native_regions=True,
        native_figure_crops=False,
        intra_page=True,
        preview=True,
        residency="azure-sold-direct (Preview) or first-party API",
    )

    def __init__(
        self,
        client: MistralOCRClient,
        model: str = "mistral-ocr-latest",
        pages_per_request: int = 30,
        usd_per_page: float = USD_PER_PAGE,
        drawing_area_ratio: float = 0.25,
        schedule_area_ratio: float = 0.30,
    ) -> None:
        self.client = client
        self.model = model
        self.pages_per_request = pages_per_request
        self.usd_per_page = usd_per_page
        self.drawing_area_ratio = drawing_area_ratio
        self.schedule_area_ratio = schedule_area_ratio

    # -- request ------------------------------------------------------------

    def _call(self, data: bytes, page_range: list[int] | None) -> Any:
        import base64

        document = {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{base64.b64encode(data).decode()}",
        }
        return self.client.process(
            model=self.model,
            document=document,
            pages=page_range,
            include_blocks=True,
            table_format="markdown",
            confidence_scores_granularity="page",
        )

    def _page_batches(self, data: bytes) -> list[list[int] | None]:
        """Chunk to the service page cap. Returns ``[None]`` when unknown/small."""
        page_count = _pdf_page_count(data)
        if page_count is None or page_count <= self.pages_per_request:
            return [None]
        return [
            list(range(start, min(start + self.pages_per_request, page_count)))
            for start in range(0, page_count, self.pages_per_request)
        ]

    # -- mapping ------------------------------------------------------------

    def analyze(self, file_id: str, data: bytes, source_uri: str | None = None) -> DocumentAnalysis:
        batches = self._page_batches(data)
        warnings: list[str] = []
        if len(batches) > 1:
            warnings.append(
                f"document exceeds the {self.pages_per_request}-page service limit; "
                f"split into {len(batches)} requests and offsets rebased"
            )
        if self.capabilities.preview:
            warnings.append(f"{self.model} is in Preview on Azure; not production-committable")
        if self.capabilities.languages == ("en",):
            warnings.append(
                "Azure-hosted Mistral OCR is English-only; non-English packages must "
                "route to another producer"
            )

        chunks: list[str] = []
        regions: list[Region] = []
        headings: list[SectionNode] = []
        furniture: list[Span] = []
        cursor = 0
        page_number = 0
        calls = 0

        for page_range in batches:
            response = self._call(data, page_range)
            calls += 1
            for page in getattr(response, "pages", []) or []:
                page_number += 1
                markdown = getattr(page, "markdown", "") or ""
                page_regions, page_headings, page_furniture = self._map_page(
                    markdown, page, page_number, base_offset=cursor
                )
                regions.extend(page_regions)
                headings.extend(page_headings)
                furniture.extend(page_furniture)
                chunks.append(markdown)
                cursor += len(markdown) + len(PAGE_SEPARATOR)

        content = PAGE_SEPARATOR.join(chunks)
        segments = self._build_segments(file_id, regions, page_number)

        index = SectionIndex(
            nodes=sorted(headings, key=lambda n: n.offset),
            strategy="mistral-title-blocks",
            role_headings=len(headings),
        )
        for segment in segments:
            segment.section_root = index.root_for(
                segment.start, inherits=segment.content_type is not ContentType.DRAWING
            )

        return DocumentAnalysis(
            file_id=file_id,
            content=content,
            page_count=page_number,
            content_sha256=hashlib.sha256(data).hexdigest(),
            segments=segments,
            section_index=index,
            producer=self.name,
            cost=ProducerCost(
                pages=page_number,
                api_calls=calls,
                usd_estimate=round(page_number * self.usd_per_page, 6),
            ),
            furniture_spans=furniture,
            warnings=warnings,
        )

    def _map_page(
        self, markdown: str, page: Any, page_number: int, base_offset: int
    ) -> tuple[list[Region], list[SectionNode], list[Span]]:
        """Blocks are ordered, so locate each one with a forward-moving cursor.

        Scanning forward rather than searching globally is what makes repeated
        strings (``Tag``, ``kW``) resolve to the right occurrence.
        """
        regions: list[Region] = []
        headings: list[SectionNode] = []
        furniture: list[Span] = []
        claimed: list[tuple[int, int]] = []
        search_from = 0

        blocks = getattr(page, "blocks", None) or []
        for index, block in enumerate(blocks):
            label = (getattr(block, "type", None) or getattr(block, "label", "") or "").lower()
            text = getattr(block, "content", None) or getattr(block, "text", "") or ""
            if not text:
                continue

            found = markdown.find(text, search_from)
            if found < 0:  # markdown normalisation moved it; fall back to a global search
                found = markdown.find(text)
            if found < 0:
                continue
            search_from = found + len(text)

            absolute = [(base_offset + found, len(text))]
            if label in FURNITURE_BLOCKS:
                claimed += absolute
                furniture += [Span(offset=o, length=l) for o, l in absolute]
                continue

            kept = subtract(absolute, claimed)
            if not kept:
                continue
            claimed += kept

            if label in HEADING_BLOCKS:
                headings.append(
                    SectionNode(
                        offset=kept[0][0],
                        heading=text.strip().lstrip("#").strip(),
                        page=page_number,
                        level=_heading_level(text),
                    )
                )

            regions.append(
                Region(
                    kind=BLOCK_TO_KIND.get(label, ContentType.TEXT),
                    ref=f"block[{index}:{label or 'text'}]",
                    page=page_number,
                    spans=[Span(offset=o, length=l) for o, l in kept],
                    polygon=_polygon(block),
                )
            )

        return regions, headings, furniture

    def _build_segments(
        self, file_id: str, regions: list[Region], page_count: int
    ) -> list[Segment]:
        """Page type = dominant region kind by claimed characters."""
        by_page: dict[int, list[Region]] = {}
        for region in regions:
            by_page.setdefault(region.page, []).append(region)

        labelled: list[tuple[int, ContentType, float]] = []
        for page in range(1, page_count + 1):
            page_regions = by_page.get(page, [])
            if not page_regions:
                labelled.append((page, ContentType.OTHER, 1.0))
                continue
            totals: dict[ContentType, int] = {}
            for region in page_regions:
                totals[region.kind] = totals.get(region.kind, 0) + region.char_count
            ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
            top, top_chars = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else 0
            confidence = (top_chars - runner_up) / top_chars if top_chars else 0.0
            labelled.append((page, top, round(max(0.0, min(1.0, confidence)), 3)))

        segments: list[Segment] = []
        for page, kind, confidence in labelled:
            last = segments[-1] if segments else None
            if last and last.content_type == kind and last.last_page == page - 1:
                last.last_page = page
                last.page_confidences.append(confidence)
            else:
                segments.append(
                    Segment(
                        segment_id=f"{file_id}-seg-{len(segments) + 1:03d}",
                        file_id=file_id,
                        first_page=page,
                        last_page=page,
                        content_type=kind,
                        confidence=confidence,
                        page_confidences=[confidence],
                        producer=self.name,
                    )
                )

        for segment in segments:
            segment.confidence = round(min(segment.page_confidences), 3)
            segment.regions = sorted(
                (r for p in segment.pages for r in by_page.get(p, [])),
                key=lambda r: r.start,
            )
        return segments

    def figure_image(self, analysis: DocumentAnalysis, figure_id: str) -> bytes | None:
        """No server-side crops. Drawings must be rasterised locally before tiling."""
        return None


def _heading_level(text: str) -> int:
    stripped = text.lstrip()
    return min(stripped.count("#", 0, 6) or 1, 6)


def _polygon(block: Any) -> list[float] | None:
    bbox = getattr(block, "bbox", None) or getattr(block, "bounding_box", None)
    if not bbox:
        return None
    try:
        x0, y0, x1, y1 = (
            bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
        ) if isinstance(bbox, dict) else (
            bbox.top_left_x, bbox.top_left_y, bbox.bottom_right_x, bbox.bottom_right_y
        )
    except (KeyError, AttributeError, TypeError, ValueError):
        return None
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def _pdf_page_count(data: bytes) -> int | None:
    try:
        import fitz

        with fitz.open(stream=data, filetype="pdf") as document:
            return document.page_count
    except Exception:  # noqa: BLE001 - page count is an optimisation, not a requirement
        return None
