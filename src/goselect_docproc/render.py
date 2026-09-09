"""Render source PDF regions at a controlled resolution for drawing extraction."""

from __future__ import annotations

import logging
from io import BytesIO
from math import isfinite

import pymupdf
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

DEFAULT_DPI = 300


def parse_source(source: str | None) -> tuple[int | None, list[float] | None]:
    """Read the first native document source expression."""
    first = (source or "").split(";")[0].strip()
    if not first.startswith("D(") or not first.endswith(")"):
        return None, None
    try:
        values = [float(part) for part in first[2:-1].split(",")]
        page, *polygon = values
        if not all(isfinite(value) for value in values) or page < 1 or not page.is_integer():
            return None, None
        if polygon and (len(polygon) < 8 or len(polygon) % 2):
            return None, None
        return int(page), polygon or None
    except ValueError:
        return None, None


def render_page(data: bytes, page_number: int, dpi: int = DEFAULT_DPI) -> bytes | None:
    """Render one 1-based PDF page to PNG bytes at ``dpi``."""
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - pymupdf is an optional extra
        log.warning("pymupdf not installed; falling back to service figure crops")
        return None

    try:
        with fitz.open(stream=data, filetype="pdf") as document:
            if not 1 <= page_number <= document.page_count:
                return None
            page = document.load_page(page_number - 1)
            pixmap = page.get_pixmap(dpi=dpi)
            return pixmap.tobytes("png")
    except Exception as exc:  # noqa: BLE001 - a page that will not render is not a job failure
        log.warning("page %d could not be rendered at %d dpi: %s", page_number, dpi, exc)
        return None


def render_pages(
    data: bytes, first: int, last: int, dpi: int = DEFAULT_DPI,
    *, source: str | None = None, unit: str | None = None,
) -> dict[str, bytes]:
    """Crop and mask source polygons, or render full pages when no source exists."""
    images: dict[str, bytes] = {}
    regions = [parse_source(part) for part in source.split(";")] if source else [
        (page_number, None) for page_number in range(first, last + 1)
    ]
    with pymupdf.open(stream=data, filetype="pdf") as document:
        for index, (page_number, polygon) in enumerate(regions):
            if page_number is None or not first <= page_number <= last or page_number > len(document):
                raise ValueError("Invalid drawing source page")
            page = document[page_number - 1]
            clip = None
            if polygon:
                if unit != "inch":
                    raise ValueError(f"Unsupported PDF source unit: {unit!r}")
                points = list(zip(polygon[::2], polygon[1::2]))
                clip = pymupdf.Rect(
                    min(point[0] for point in points) * 72,
                    min(point[1] for point in points) * 72,
                    max(point[0] for point in points) * 72,
                    max(point[1] for point in points) * 72,
                ) & page.rect
                if clip.is_empty:
                    raise ValueError("Drawing source lies outside the PDF page")
            pixmap = page.get_pixmap(dpi=dpi, clip=clip)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            if polygon:
                mask = Image.new("L", image.size)
                ImageDraw.Draw(mask).polygon(
                    [(horizontal * dpi - pixmap.x, vertical * dpi - pixmap.y) for horizontal, vertical in points],
                    fill=255,
                )
                image = Image.composite(image, Image.new("RGB", image.size, "white"), mask)
            output = BytesIO()
            image.save(output, format="PNG")
            images[f"page-{page_number}-region-{index}"] = output.getvalue()
    return images
