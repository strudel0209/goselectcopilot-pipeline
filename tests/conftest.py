"""Fakes for the Document Intelligence object model.

Everything the pipeline needs from ``AnalyzeResult`` is duck-typed, so the pure
logic is testable with no Azure dependency and no spend.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest


def span(offset: int, length: int) -> NS:
    return NS(offset=offset, length=length)


def poly(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def region(page: int, polygon: list[float]) -> NS:
    return NS(page_number=page, polygon=polygon)


def paragraph(offset, length, page, content, role=None, polygon=None):
    return NS(
        spans=[span(offset, length)],
        bounding_regions=[region(page, polygon or poly(1, 1, 5, 1.2))],
        content=content,
        role=role,
    )


def table(offset, length, page, polygon, rows=3, cols=3):
    return NS(
        spans=[span(offset, length)],
        bounding_regions=[region(page, polygon)],
        row_count=rows,
        column_count=cols,
    )


def figure(fid, offset, length, page, polygon):
    return NS(id=fid, spans=[span(offset, length)], bounding_regions=[region(page, polygon)])


def word(content, polygon):
    return NS(content=content, polygon=polygon)


def page(number, width=8.5, height=11.0, words=(), lines=(), unit="inch", angle=0.0):
    return NS(
        page_number=number,
        width=width,
        height=height,
        unit=unit,
        angle=angle,
        words=list(words),
        lines=list(lines),
        selection_marks=[],
    )


def di_section(elements, offset, length):
    """A node of Document Intelligence's ``sections`` tree.

    ``elements`` are JSON pointers such as ``/paragraphs/3`` or ``/sections/2``.
    """
    return NS(elements=list(elements), spans=[span(offset, length)])


def analyze_result(content, pages, paragraphs=(), tables=(), figures=(), styles=(), sections=()):
    return NS(
        content=content,
        pages=list(pages),
        paragraphs=list(paragraphs),
        tables=list(tables),
        figures=list(figures),
        styles=list(styles),
        sections=list(sections),
    )


@pytest.fixture
def mixed_page_result():
    """One page holding narrative, a schedule table and a drawing figure."""
    content = (
        "## Mechanical features\n"          # 0   .. 24
        "Motors are IP55 as standard.\n"    # 24  .. 52
        "<table><tr><th>Tag</th></tr></table>\n"  # 52 .. 89
        "<figure>VFD-401</figure>\n"        # 89  .. 114
        '<!-- PageFooter="ABB" -->\n'       # 114 .. 140
    )
    return analyze_result(
        content=content,
        pages=[page(1, words=[word("Motors", poly(1, 1, 1.5, 1.15))])],
        paragraphs=[
            paragraph(0, 23, 1, "## Mechanical features", role="sectionHeading"),
            paragraph(24, 27, 1, "Motors are IP55 as standard."),
            paragraph(59, 3, 1, "Tag"),  # inside the table span - must be subtracted
            paragraph(114, 25, 1, 'PageFooter="ABB"', role="pageFooter"),
        ],
        tables=[table(52, 36, 1, poly(1.0, 3.0, 7.5, 5.0))],
        figures=[figure("1.1", 89, 24, 1, poly(1.0, 6.0, 7.5, 9.5))],
    )


@pytest.fixture
def drawing_sheet_result():
    """A drawing whose border is the figure, with a title block table inside it."""
    sheet = poly(0.0, 0.0, 11.0, 8.5)
    title_block = poly(7.5, 6.5, 10.5, 8.0)
    content = "<figure>VFD-401 M-401</figure>REV A  SHEET 1 OF 3"
    return analyze_result(
        content=content,
        pages=[page(1, width=11.0, height=8.5)],
        paragraphs=[],
        tables=[table(29, 19, 1, title_block, rows=4, cols=2)],
        figures=[figure("1.1", 0, 29, 1, sheet)],
    )


@pytest.fixture
def stapled_package_result():
    """A specification with a drawing sheet stapled behind it.

    This is the Howey shape: Document Intelligence reports two independent root
    subtrees, so a value on the drawing must never resolve to a specification
    clause. Offsets are spelled out because the whole mechanism is offset-based.
    """
    content = (
        "SECTION 16370 VFD\n"          # 0  .. 17
        "3.05 TESTS\n"                 # 18 .. 28
        "Megger the terminals.\n"      # 29 .. 50
        "2x 124 Amp\n"                 # 51 .. 61
    )
    return analyze_result(
        content=content,
        pages=[page(1), page(2, width=11.0, height=8.5)],
        paragraphs=[
            paragraph(0, 17, 1, "SECTION 16370 VFD", role="title"),
            paragraph(18, 10, 1, "3.05 TESTS", role="sectionHeading"),
            paragraph(29, 21, 1, "Megger the terminals."),
            paragraph(51, 10, 2, "2x 124 Amp", role="title"),
        ],
        sections=[
            di_section(["/sections/1", "/sections/3"], 0, 62),  # root
            di_section(["/paragraphs/0", "/sections/2"], 0, 50),  # the specification
            di_section(["/paragraphs/1", "/paragraphs/2"], 18, 32),  # clause 3.05
            di_section(["/paragraphs/3"], 51, 11),  # the drawing sheet
        ],
    )
