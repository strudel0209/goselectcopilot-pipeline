"""Content Understanding producer — Azure-native classify, split and route.

``contentCategories`` classifies **and splits** a package in one call, with
categories defined by description rather than training data. That removes the
labelling burden the DI custom-classifier path carries.

Everything here speaks the ``azure-ai-contentunderstanding`` models directly.
There is no hand-rolled REST client and no dict/namespace duck-typing: the tests
build the same ``AnalysisResult`` the service returns, so a field that moves
breaks a test instead of silently reading ``None``.

**Granularity.** Under the GA API the classifier's minimum unit is one page. The
``2026-06-01-preview`` API adds ``allow_in_page_segments``, which lets a segment
cover part of a page — the case that matters here, because a schedule is often
printed on the same sheet as a drawing. Off by default until it is measured.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from azure.ai.contentunderstanding.models import (
    AnalysisInput,
    AnalysisResult,
    ContentAnalyzer,
    ContentAnalyzerConfig,
    ContentCategoryDefinition,
    ContentFieldSchema,
    ContentSpan,
    DocumentContent,
)

from ..contracts import ContentType, Region, SectionNode, Segment, Span
from ..sections import SELF_TITLING, ParagraphRef, SectionIndex, SectionRef, tree_index
from ..spans import subtract
from .base import DocumentAnalysis, ProducerCapabilities, ProducerCost, register

log = logging.getLogger(__name__)

FURNITURE_ROLES = {"pageHeader", "pageFooter", "pageNumber"}

USD_PER_PAGE = 0.010

DEFAULT_ANALYZER_ID = "goselectRouterV3"

# gpt-4.1 retires October 2026. Measured equal to the flagship on specification
# prose, so the mini is the default here too.
DEFAULT_COMPLETION_MODEL = "gpt-5.4-mini"

# The general "description properties" cap. The classifier page also quotes 120
# characters for name + description, but neither API version encodes a length
# constraint on ContentCategoryDefinition.description and longer descriptions are
# accepted in practice, so that figure is not treated as a gate here.
MAX_DESCRIPTION_CHARS = 1024

# Below this a tree is too thin to trust, and the flat role scan is the better
# guess. Matches the Document Intelligence path so the two are comparable.
MIN_TREE_HEADINGS = 3

CATEGORY_TO_KIND: dict[str, ContentType] = {
    "text": ContentType.TEXT,
    "schedule": ContentType.SCHEDULE,
    "drawing": ContentType.DRAWING,
    "plan": ContentType.PLAN,
    "other": ContentType.OTHER,
}

# Categories whose segments go straight into field extraction. DRAWING is absent
# on purpose: it needs native-resolution tiles the service will not produce.
# PLAN is absent because it is out of scope.
FIELD_ROUTED_CATEGORIES = frozenset({"text", "schedule"})

# Categories describe what a human sees, not what the pipeline does with it.
# `drawing` and `plan` are the load-bearing split: both are sheets with a border
# and a title block, but only one carries connectivity.
DOCUMENT_CATEGORIES: dict[str, ContentCategoryDefinition] = {
    "text": ContentCategoryDefinition(
        description=(
            "Prose and symbol keys: general notes, legends, abbreviation lists, "
            "specifications, method statements or correspondence. Mostly running text, "
            "not a table or a drawing."
        )
    ),
    "schedule": ContentCategoryDefinition(
        description=(
            "Any tabular schedule or list with one row per item and repeating columns: "
            "equipment, starters, panels, cables, instruments, I/O points, parts lists, "
            "relay settings, datasheets or test records. Usually titled SCHEDULE, LIST "
            "or TABLE, and often printed on the same sheet as a drawing."
        )
    ),
    "drawing": ContentCategoryDefinition(
        description=(
            "A schematic drawing: symbols joined by lines to show how things connect, "
            "not where they physically are, and not to scale. Covers electrical one-line "
            "and single line diagrams, network and communications diagrams, and control "
            "or elementary ladder diagrams. The drawn body only - a tabular schedule on "
            "the same sheet is a schedule, not the drawing."
        )
    ),
    "plan": ContentCategoryDefinition(
        description=(
            "A scaled view of physical space or hardware, with dimensions: site and "
            "building plans, equipment layouts, routing and grounding plans, and "
            "construction details such as sections, elevations and profile views. Shows "
            "where things physically are and how they are built, not how they connect."
        )
    ),
    # Without a catch-all, content is forced into one of the categories above.
    "other": ContentCategoryDefinition(
        description="Content that matches none of the other categories, including blank pages."
    ),
}


def field_analyzer(
    schema: ContentFieldSchema, completion_model: str = DEFAULT_COMPLETION_MODEL
) -> ContentAnalyzer:
    """Extracts the agreed contract. Grounding is on: a value the service cannot
    place on a page is one a reviewer cannot check."""
    return ContentAnalyzer(
        base_analyzer_id="prebuilt-document",
        description="GoSelect Copilot fields: the agreed VFD/motor extraction contract",
        config=ContentAnalyzerConfig(
            return_details=True,
            enable_formula=False,
            estimate_field_source_and_confidence=True,
        ),
        field_schema=schema,
        models={"completion": completion_model},
    )


def schedule_analyzer(completion_model: str = DEFAULT_COMPLETION_MODEL) -> ContentAnalyzer:
    from ..field_schema import schedule_schema

    return field_analyzer(schedule_schema(), completion_model)


def text_analyzer(completion_model: str = DEFAULT_COMPLETION_MODEL) -> ContentAnalyzer:
    from ..field_schema import text_schema

    return field_analyzer(text_schema(), completion_model)


def router_analyzer(
    completion_model: str = DEFAULT_COMPLETION_MODEL,
    *,
    in_page_segments: bool = False,
    field_analyzer_ids: Mapping[str, str] | None = None,
) -> ContentAnalyzer:
    """The classify-and-split analyzer.

    ``field_analyzer_ids`` maps a category to its own sub-analyzer, so prose and
    grids are extracted against different schemas in the same call. One shared
    schema does not work: the full contract is 112 fields, and asking for all of
    them per row truncates the document before the motor block is reached.
    Drawings are deliberately not routed - they need native resolution tiles the
    service will not produce - and plans are out of scope.
    """
    routes = field_analyzer_ids or {}
    categories = {
        name: ContentCategoryDefinition(
            description=definition.description,
            analyzer_id=routes.get(name),
        )
        for name, definition in DOCUMENT_CATEGORIES.items()
    }
    return ContentAnalyzer(
        base_analyzer_id="prebuilt-document",
        description="GoSelect Copilot router: classify and split a specification package",
        config=ContentAnalyzerConfig(
            return_details=True,
            enable_segment=True,
            allow_in_page_segments=in_page_segments,
            # Must be declared false: the service default is true, and on a drawing the
            # box around a tag reads as a radical sign - VFD-401 becomes \\sqrt{150-401}.
            enable_formula=False,
            estimate_field_source_and_confidence=True,
            content_categories=categories,
        ),
        models={"completion": completion_model},
    )


@runtime_checkable
class AnalyzeClient(Protocol):
    """The slice of ``ContentUnderstandingClient`` this producer uses."""

    def begin_analyze_binary(self, analyzer_id: str, binary_input: bytes, **kwargs: Any) -> Any: ...

    def begin_analyze(self, analyzer_id: str, **kwargs: Any) -> Any: ...


def ensure_analyzer(
    client: Any, analyzer_id: str, analyzer: ContentAnalyzer | None = None
) -> ContentAnalyzer:
    """Create the analyzer, replacing any existing one.

    ``allow_replace`` is the service performing delete-then-create itself. A bare
    create returns 409 ModelExists, and PATCH reaches only description and tags,
    so a changed category set cannot be applied in place. An analyzer left on a
    stale taxonomy returns categories the routing table has no entry for, and
    every segment silently downgrades to OTHER.
    """
    poller = client.begin_create_analyzer(
        analyzer_id, analyzer or router_analyzer(), allow_replace=True
    )
    created = poller.result()
    log.info("analyzer %s ready", analyzer_id)
    return created


@register("content-understanding")
class ContentUnderstandingProducer:
    capabilities = ProducerCapabilities(
        native_regions=True,
        native_figure_crops=False,
        intra_page=True,  # classifier is page-level unless allow_in_page_segments is on
        residency="azure-native",
    )

    def __init__(
        self,
        client: AnalyzeClient,
        analyzer_id: str = DEFAULT_ANALYZER_ID,
        usd_per_page: float = USD_PER_PAGE,
    ) -> None:
        self.client = client
        self.analyzer_id = analyzer_id
        self.usd_per_page = usd_per_page

    def analyze(self, file_id: str, data: bytes, source_uri: str | None = None) -> DocumentAnalysis:
        response = self._call(data, source_uri)
        item = _first_document(response)
        if item is None:
            raise RuntimeError("content understanding returned no document content")

        content = item.markdown or ""
        pages = list(item.pages or [])
        page_of = _page_lookup(pages)

        claims = _classify_elements(item, page_of)
        blocks = list(item.segments or [])
        segments = [
            self._segment(file_id, index, block, claims)
            for index, block in enumerate(blocks, start=1)
        ]

        index_ = _section_index_from(item, page_of)
        for segment in segments:
            segment.section_root = index_.root_for(
                segment.start, inherits=segment.content_type not in SELF_TITLING
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
            figure_ids_by_segment={},
            fields_by_segment=_fields_by_segment(response, item, blocks, segments),
            furniture_spans=[Span(offset=o, length=l) for o, l, _, kind in claims if kind is None],
            native=item,
        )

    def _call(self, data: bytes, source_uri: str | None) -> AnalysisResult:
        """A URL is the documented input; binary upload is first-class too."""
        if source_uri and source_uri.startswith(("http://", "https://")):
            poller = self.client.begin_analyze(
                self.analyzer_id, inputs=[AnalysisInput(url=source_uri)]
            )
        else:
            poller = self.client.begin_analyze_binary(self.analyzer_id, binary_input=data)
        return poller.result()

    def _segment(self, file_id: str, index: int, block: Any, claims: list[_Claim]) -> Segment:
        span = block.span or ContentSpan(offset=0, length=0)
        offset = int(span.offset or 0)
        first = int(block.start_page_number or 1)
        last = int(block.end_page_number or first)
        category = block.category or "other"

        content_type = CATEGORY_TO_KIND.get(category)
        if content_type is None:
            # Almost always a deployed analyzer that predates the routing table.
            log.warning(
                "category %r is not in the routing table %s; routing to OTHER. "
                "Re-run setup-analyzer if the taxonomy changed.",
                category,
                sorted(CATEGORY_TO_KIND),
            )
            content_type = ContentType.OTHER

        confidence = block.confidence if block.confidence is not None else 1.0
        return Segment(
            segment_id=f"{file_id}-seg-{index:03d}",
            file_id=file_id,
            first_page=first,
            last_page=last,
            content_type=content_type,
            confidence=round(float(confidence), 3),
            page_confidences=[1.0] * (last - first + 1),
            regions=_regions_for(claims, offset, offset + int(span.length or 0), content_type),
            producer=self.name,
        )

    def figure_image(self, analysis: DocumentAnalysis, figure_id: str) -> bytes | None:
        """Figure output is a description plus chart.js or mermaid, never bytes."""
        return None


# ---------------------------------------------------------------------------
# response mapping
# ---------------------------------------------------------------------------

# (offset, length, page, kind). ``kind`` is None for declared page furniture.
_Claim = tuple[int, int, int, ContentType | None]


def _first_document(response: AnalysisResult) -> DocumentContent | None:
    for content in response.contents or []:
        if getattr(content, "markdown", None) is not None:
            return content  # type: ignore[return-value]
    return None


def _fields_by_segment(
    response: AnalysisResult,
    router_content: DocumentContent,
    blocks: list[Any],
    segments: list[Segment],
) -> dict[str, Any]:
    """Category routing returns the router's own content plus one extra entry per
    routed segment. Match those back by the service's own segment id, falling
    back to the page range when a routed entry reports no segments.

    A routed entry can name several native segments: with in-page segmentation a
    sheet carrying both a diagram and a grid reports both. Its own ``category``
    is what disambiguates - matching the first id instead attached a schedule's
    rows to the drawing beside it, whose extractor ignores them, and the rows
    were silently lost.
    """
    by_native = {
        block.segment_id: segment
        for block, segment in zip(blocks, segments)
        if block.segment_id
    }
    by_pages: dict[tuple[int, int], list[Segment]] = {}
    for segment in segments:
        by_pages.setdefault((segment.first_page, segment.last_page), []).append(segment)

    out: dict[str, Any] = {}
    for content in response.contents or []:
        fields = getattr(content, "fields", None)
        if content is router_content or not fields:
            continue
        wanted = CATEGORY_TO_KIND.get(getattr(content, "category", None) or "")
        candidates = [
            by_native[b.segment_id]
            for b in (getattr(content, "segments", None) or [])
            if b.segment_id in by_native
        ] or by_pages.get((content.start_page_number, content.end_page_number), [])

        owner = next(
            (c.segment_id for c in candidates if wanted and c.content_type is wanted),
            candidates[0].segment_id if candidates else None,
        )
        if owner:
            out[owner] = fields
        else:
            log.warning(
                "routed fields on pages %s-%s match no segment; they will be dropped",
                content.start_page_number,
                content.end_page_number,
            )
    return out


def _page_lookup(pages: list[Any]) -> list[tuple[int, int, int]]:
    """``(start, end, pageNumber)`` so an element span resolves to a page."""
    out: list[tuple[int, int, int]] = []
    for page in pages:
        number = int(page.page_number or 0)
        for span in page.spans or []:
            offset = int(span.offset or 0)
            out.append((offset, offset + int(span.length or 0), number))
    return sorted(out)


def _page_for(offset: int, lookup: list[tuple[int, int, int]]) -> int:
    for start, end, number in lookup:
        if start <= offset < end:
            return number
    return lookup[0][2] if lookup else 1


def _span_of(element: Any) -> tuple[int, int] | None:
    """Layout elements carry ``span``; pages carry ``spans``."""
    span = getattr(element, "span", None)
    if span is None:
        spans = getattr(element, "spans", None) or []
        span = spans[0] if spans else None
    if span is None:
        return None
    return int(span.offset or 0), int(span.length or 0)


def _classify_elements(item: DocumentContent, page_of: list[tuple[int, int, int]]) -> list[_Claim]:
    """Tables and figures claim first; unclaimed paragraphs are narrative.

    ``None`` as the kind marks page furniture, which is dropped by design and
    declared so the coverage proof does not count it as loss.
    """
    claimed: list[tuple[int, int]] = []
    out: list[_Claim] = []

    for element, kind in (
        *((t, ContentType.SCHEDULE) for t in (item.tables or [])),
        *((f, ContentType.DRAWING) for f in (item.figures or [])),
    ):
        span = _span_of(element)
        if not span or not span[1]:
            continue
        for offset, length in subtract([span], claimed):
            claimed.append((offset, length))
            out.append((offset, length, _page_for(offset, page_of), kind))

    for paragraph in item.paragraphs or []:
        span = _span_of(paragraph)
        if not span or not span[1]:
            continue
        if paragraph.role in FURNITURE_ROLES:
            claimed.append(span)
            out.append((span[0], span[1], _page_for(span[0], page_of), None))
            continue
        for offset, length in subtract([span], claimed):
            claimed.append((offset, length))
            out.append((offset, length, _page_for(offset, page_of), ContentType.TEXT))

    return sorted(out)


def _regions_for(
    claims: list[_Claim], start: int, end: int, segment_type: ContentType
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


def _section_index_from(item: DocumentContent, page_of: list[tuple[int, int, int]]) -> SectionIndex:
    """``sections`` is a real tree with nesting depth and document boundaries;
    heading-role paragraphs are the flat fallback when the service returns none."""
    paragraphs: list[ParagraphRef | None] = []
    for paragraph in item.paragraphs or []:
        span = _span_of(paragraph)
        paragraphs.append(
            (span[0], _page_for(span[0], page_of), paragraph.content or "") if span else None
        )

    sections: list[SectionRef] = [
        (_span_of(section), list(section.elements or [])) for section in (item.sections or [])
    ]
    nodes, boundaries = tree_index(sections, paragraphs)
    if len(nodes) >= MIN_TREE_HEADINGS:
        return SectionIndex(
            nodes=nodes,
            strategy="content-understanding-sections",
            role_headings=len(nodes),
            boundaries=tuple(boundaries),
        )

    if sections and not nodes:
        # The service analysed structure and found none. Scanning roles anyway is
        # how every equipment label on a one-line diagram becomes a clause.
        return SectionIndex(nodes=[], strategy="content-understanding-sections-flat", role_headings=0)

    role_nodes: list[SectionNode] = []
    for paragraph, ref in zip(item.paragraphs or [], paragraphs):
        if ref is None or paragraph.role not in ("title", "sectionHeading"):
            continue
        offset, page, content = ref
        heading = _heading(content)
        if heading:
            role_nodes.append(SectionNode(offset=offset, heading=heading, page=page, level=1))

    return SectionIndex(
        nodes=sorted(role_nodes, key=lambda n: n.offset),
        strategy="content-understanding-roles" if role_nodes else "content-understanding-none",
        role_headings=len(role_nodes),
    )


def _heading(content: str) -> str:
    return (content or "").strip().lstrip("#").strip()
