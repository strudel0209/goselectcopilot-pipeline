"""Bounding-polygon helpers.

Document Intelligence polygons are ``[x1,y1,x2,y2,x3,y3,x4,y4]`` clockwise from
top-left, in **inches** for PDF/Office and **pixels** for images. Nothing here
assumes a unit; callers scale at render time.
"""

from __future__ import annotations

from collections.abc import Iterable

BBox = tuple[float, float, float, float]


def bbox(polygon: Iterable[float]) -> BBox:
    points = list(polygon)
    xs, ys = points[0::2], points[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def area(polygon: Iterable[float]) -> float:
    x0, y0, x1, y1 = bbox(polygon)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def height(polygon: Iterable[float]) -> float:
    _, y0, _, y1 = bbox(polygon)
    return y1 - y0


def contains(outer: Iterable[float], inner: Iterable[float], tolerance: float = 0.02) -> bool:
    ox0, oy0, ox1, oy1 = bbox(outer)
    ix0, iy0, ix1, iy1 = bbox(inner)
    pad = tolerance * max(ox1 - ox0, oy1 - oy0)
    return ox0 - pad <= ix0 and oy0 - pad <= iy0 and ix1 <= ox1 + pad and iy1 <= oy1 + pad


def is_axis_aligned(polygon: Iterable[float]) -> bool:
    """True when the top edge is horizontal, within a fraction of glyph height.

    Unit-agnostic, so it works for inch (PDF) and pixel (image) polygons alike.
    """
    points = list(polygon)
    if len(points) < 8:
        return True
    tolerance = max(height(points) * 0.25, 1e-6)
    return abs(points[1] - points[3]) <= tolerance


def on_page(element: object, page_number: int) -> bool:
    regions = getattr(element, "bounding_regions", None) or []
    return any(r.page_number == page_number for r in regions)


def regions_on_page(element: object, page_number: int) -> list:
    regions = getattr(element, "bounding_regions", None) or []
    return [r for r in regions if r.page_number == page_number]


def polygon_on_page(element: object, page_number: int) -> list[float] | None:
    found = regions_on_page(element, page_number)
    return list(found[0].polygon) if found else None
