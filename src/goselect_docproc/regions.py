"""Intra-page region splitting by span subtraction.

No Azure classifier splits inside a page - Content Understanding states the
minimum unit is one page. A sheet holding a schedule *and* an inset diagram is
therefore split here, deterministically, with no model call.

Two rules make it exact rather than a proximity guess:

* **Claim order by content type.** On a drawing sheet the title block, revision
  table and BOM are *tables* that belong to the drawing, so figures claim first.
* **Containment.** A figure absorbs any table whose bounding box sits inside it.

Note for v4.0 (2024-11-30): figure and table ``boundingRegions`` cover only core
content and **exclude captions and footnotes**, so captions correctly fall
through to narrative rather than being swallowed.
"""

from __future__ import annotations

from typing import Any

from . import geometry as geo
from .contracts import ContentType, Region, Span
from .spans import overlaps, subtract

FURNITURE_ROLES = {"pageHeader", "pageFooter", "pageNumber"}

CLAIM_ORDER: dict[ContentType, tuple[str, ...]] = {
    ContentType.DRAWING: ("figure", "table"),
    ContentType.SCHEDULE: ("table", "figure"),
    ContentType.TEXT: ("table", "figure"),
    ContentType.OTHER: ("table", "figure"),
}

MIN_FIGURE_AREA_RATIO = 0.10


def _spans_of(element: Any) -> list[tuple[int, int]]:
    return [(s.offset, s.length) for s in (getattr(element, "spans", None) or [])]


def page_regions(
    result: Any,
    page_number: int,
    segment_type: ContentType = ContentType.TEXT,
    min_figure_area_ratio: float = MIN_FIGURE_AREA_RATIO,
) -> list[Region]:
    page = next(p for p in result.pages if p.page_number == page_number)
    page_area = (page.width or 1) * (page.height or 1)

    tables = [(i, t) for i, t in enumerate(result.tables or []) if geo.on_page(t, page_number)]
    figures = [
        f
        for f in (result.figures or [])
        if geo.on_page(f, page_number)
        and geo.area(geo.polygon_on_page(f, page_number) or []) / page_area >= min_figure_area_ratio
    ]  # smaller than this is a logo, stamp or signature

    regions: list[Region] = []
    claimed: list[tuple[int, int]] = []

    def take_tables() -> None:
        for index, table in tables:
            kept = subtract(_spans_of(table), claimed)
            if not kept:
                continue
            claimed.extend(kept)
            regions.append(
                Region(
                    kind=ContentType.SCHEDULE,
                    ref=f"table[{index}]",
                    page=page_number,
                    spans=[Span(offset=o, length=l) for o, l in kept],
                    polygon=geo.polygon_on_page(table, page_number),
                    rows=getattr(table, "row_count", None),
                    columns=getattr(table, "column_count", None),
                )
            )

    def take_figures() -> None:
        for figure in figures:
            polygon = geo.polygon_on_page(figure, page_number)
            own = list(_spans_of(figure))
            absorbed = [
                index
                for index, table in tables
                if geo.contains(polygon or [], geo.polygon_on_page(table, page_number) or [])
            ]
            for index, table in tables:
                if index in absorbed:
                    own += _spans_of(table)

            kept = subtract(own, claimed)
            if not kept:
                continue
            claimed.extend(kept)
            regions.append(
                Region(
                    kind=ContentType.DRAWING,
                    ref=f"figure[{figure.id}]",
                    page=page_number,
                    spans=[Span(offset=o, length=l) for o, l in kept],
                    polygon=polygon,
                    absorbed_tables=absorbed,
                )
            )

    for step in CLAIM_ORDER.get(segment_type, CLAIM_ORDER[ContentType.TEXT]):
        (take_tables if step == "table" else take_figures)()

    narrative = [
        p
        for p in (result.paragraphs or [])
        if geo.on_page(p, page_number)
        and p.role not in FURNITURE_ROLES
        and not overlaps(_spans_of(p), claimed)
    ]
    if narrative:
        regions.append(
            Region(
                kind=ContentType.TEXT,
                ref=f"paragraphs[{len(narrative)}]",
                page=page_number,
                spans=[
                    Span(offset=o, length=l) for p in narrative for o, l in _spans_of(p)
                ],
            )
        )

    return sorted(regions, key=lambda r: r.start)


def figure_ids(result: Any, first_page: int, last_page: int) -> list[str]:
    """DI figure ids on a page range, for server-side crop retrieval."""
    return [
        f.id
        for f in (result.figures or [])
        if any(first_page <= r.page_number <= last_page for r in (f.bounding_regions or []))
        and getattr(f, "id", None)
    ]
