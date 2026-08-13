"""Segment producers — the swappable front half of the pipeline.

The **spine** (contracts, span algebra, reconciliation, tiling, validation,
coverage proof, scoring) is producer-independent and is where the differentiated
value lives. Everything that turns *bytes* into *segments and regions* sits
behind this one protocol, so it can be chosen by configuration and decided by
measurement rather than by argument.

    bytes ──▶ [ SegmentProducer ] ──▶ DocumentAnalysis ──▶ spine ──▶ JobResult
                     ▲
        di-layout · content-understanding

A producer must deliver four things. Everything downstream depends only on these:

1. ``content`` — ONE immutable string per file. Span offsets are absolute into
   it. A producer that works page-by-page is responsible for stitching pages and
   rebasing offsets, because the spine's total ordering key depends on it.
2. ``segments`` — with ``regions`` already typed and non-overlapping.
3. ``section_index`` — for breadcrumb attribution.
4. ``cost`` — pages, calls and an estimate, so the bench-off can rank on price
   as well as accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..contracts import Coverage, Segment, Span
from ..sections import SectionIndex
from ..spans import gaps, is_benign_gap, overlaps, total


@dataclass(frozen=True)
class ProducerCost:
    """Per-file cost, so the bench-off can rank on price as well as accuracy."""

    pages: int = 0
    api_calls: int = 0
    usd_estimate: float = 0.0
    notes: tuple[str, ...] = ()

    def __add__(self, other: ProducerCost) -> ProducerCost:
        return ProducerCost(
            pages=self.pages + other.pages,
            api_calls=self.api_calls + other.api_calls,
            usd_estimate=round(self.usd_estimate + other.usd_estimate, 6),
            notes=self.notes + other.notes,
        )

    @property
    def usd_per_page(self) -> float:
        return round(self.usd_estimate / self.pages, 6) if self.pages else 0.0


@dataclass
class DocumentAnalysis:
    """Producer-neutral analysis of one file. The spine consumes only this."""

    file_id: str
    content: str
    page_count: int
    content_sha256: str
    segments: list[Segment]
    section_index: SectionIndex
    producer: str
    source_uri: str = ""
    cost: ProducerCost = field(default_factory=ProducerCost)
    figure_ids_by_segment: dict[str, list[str]] = field(default_factory=dict)
    furniture_spans: list[Span] = field(default_factory=list)
    native: Any = None
    warnings: list[str] = field(default_factory=list)

    def coverage(self) -> Coverage:
        """The loss proof. Identical maths whatever produced the segments.

        Furniture is whatever the producer **declares** it dropped, plus markup
        recognisable as furniture. Declaration matters because Document
        Intelligence wraps headers in HTML comments while Mistral returns them
        as plain text with a block label - text sniffing alone would count the
        latter as content loss.
        """
        claimed = [s.as_tuple() for seg in self.segments for r in seg.regions for s in r.spans]
        declared = [s.as_tuple() for s in self.furniture_spans]
        furniture = unexplained = 0
        samples: list[str] = []
        for offset, length in gaps(len(self.content), claimed):
            text = self.content[offset : offset + length]
            if is_benign_gap(text) or overlaps([(offset, length)], declared):
                furniture += length
            else:
                unexplained += length
                if len(samples) < 5:
                    samples.append(text[:160])
        return Coverage(
            total_chars=len(self.content),
            covered_chars=total(claimed),
            furniture_chars=furniture,
            unexplained_chars=unexplained,
            unexplained_samples=samples,
        )


@runtime_checkable
class SegmentProducer(Protocol):
    """Swap by configuration; choose by measurement."""

    name: str

    def analyze(self, file_id: str, data: bytes, source_uri: str | None = None) -> DocumentAnalysis: ...

    def figure_image(self, analysis: DocumentAnalysis, figure_id: str) -> bytes | None: ...


class ProducerCapabilities:
    """Declared limits, so the bench-off fails loudly instead of silently.

    These are hard service constraints, not preferences. A producer that cannot
    accept a document must say so before it is scored against one.
    """

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        max_bytes: int | None = None,
        languages: tuple[str, ...] | None = None,
        native_regions: bool = False,
        native_figure_crops: bool = False,
        intra_page: bool = False,
        preview: bool = False,
        residency: str = "unknown",
    ) -> None:
        self.max_pages = max_pages
        self.max_bytes = max_bytes
        self.languages = languages
        self.native_regions = native_regions
        self.native_figure_crops = native_figure_crops
        self.intra_page = intra_page
        self.preview = preview
        self.residency = residency

    def check(self, page_count: int, byte_count: int) -> list[str]:
        problems: list[str] = []
        if self.max_pages and page_count > self.max_pages:
            problems.append(
                f"{page_count} pages exceeds the {self.max_pages}-page service limit; "
                "chunking is required and page offsets must be rebased"
            )
        if self.max_bytes and byte_count > self.max_bytes:
            problems.append(f"{byte_count} bytes exceeds the {self.max_bytes}-byte service limit")
        if self.preview:
            problems.append("model is in Preview; not committable for production")
        return problems


_REGISTRY: dict[str, type] = {}


def register(name: str):
    def wrap(cls):
        _REGISTRY[name] = cls
        cls.name = name
        return cls

    return wrap


def available() -> list[str]:
    return sorted(_REGISTRY)


def get(name: str) -> type:
    if name not in _REGISTRY:
        raise KeyError(f"unknown producer {name!r}; available: {available()}")
    return _REGISTRY[name]
