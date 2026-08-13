"""Span algebra.

Spans are ``(offset, length)`` character ranges into the single immutable
``content`` string that Document Intelligence returns for a file. Every
downstream guarantee - exact region splitting, order-independent reassembly,
lossless coverage - reduces to the four functions here.

All public functions take and return ``(offset, length)`` tuples. Internally
``_ranges`` works in ``(start, end)`` form; that form is never exposed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

SpanT = tuple[int, int]

# Markdown output wraps page furniture in HTML comments rather than paragraphs.
FURNITURE_MARKUP = re.compile(
    r"<!--\s*Page(?:Header|Footer|Number|Break)[^>]*-->", re.IGNORECASE
)
# Structural wrappers DI emits around figures. Not content.
STRUCTURAL_MARKUP = re.compile(r"</?(?:figure|figcaption|table|thead|tbody|tr|th|td)[^>]*>")


def _ranges(spans: Iterable[SpanT]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent spans into sorted half-open ``[start, end)``."""
    merged: list[list[int]] = []
    for offset, length in sorted(spans):
        if length <= 0:
            continue
        start, end = offset, offset + length
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def normalise(spans: Iterable[SpanT]) -> list[SpanT]:
    return [(a, b - a) for a, b in _ranges(spans)]


def overlaps(a: Iterable[SpanT], b: Iterable[SpanT]) -> bool:
    ra, rb = _ranges(a), _ranges(b)
    i = j = 0
    while i < len(ra) and j < len(rb):
        if ra[i][1] <= rb[j][0]:
            i += 1
        elif rb[j][1] <= ra[i][0]:
            j += 1
        else:
            return True
    return False


def subtract(spans: Iterable[SpanT], claimed: Iterable[SpanT]) -> list[SpanT]:
    """Interval difference: what ``spans`` keeps after ``claimed`` takes its share.

    This is the actual subtraction. A later claimant keeps the remainder rather
    than being dropped whole, so a figure overlapping a table's first two rows
    still yields the remaining rows.
    """
    blocked = _ranges(claimed)
    out: list[SpanT] = []
    for start, end in _ranges(spans):
        cursor = start
        for block_start, block_end in blocked:
            if block_end <= cursor:
                continue
            if block_start >= end:
                break
            if block_start > cursor:
                out.append((cursor, block_start - cursor))
            cursor = max(cursor, block_end)
            if cursor >= end:
                break
        if cursor < end:
            out.append((cursor, end - cursor))
    return out


def total(spans: Iterable[SpanT]) -> int:
    return sum(b - a for a, b in _ranges(spans))


def text_for(content: str, spans: Iterable[SpanT], separator: str = "\n") -> str:
    return separator.join(content[a:b] for a, b in _ranges(spans))


def gaps(content_length: int, spans: Iterable[SpanT]) -> list[SpanT]:
    """Everything in ``[0, content_length)`` that no span covers."""
    out: list[SpanT] = []
    cursor = 0
    for start, end in _ranges(spans):
        if start > cursor:
            out.append((cursor, start - cursor))
        cursor = max(cursor, end)
    if cursor < content_length:
        out.append((cursor, content_length - cursor))
    return out


def is_benign_gap(text: str) -> bool:
    """A gap is benign only if it holds nothing but page furniture or markup."""
    stripped = STRUCTURAL_MARKUP.sub("", FURNITURE_MARKUP.sub("", text))
    return not stripped.strip()
