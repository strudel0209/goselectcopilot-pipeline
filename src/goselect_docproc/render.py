"""Page rasterising for drawing extraction.

Document Intelligence serves server-side figure crops, but they come back around
1480x990 - already below the resolution a tag needs to survive. Content
Understanding returns no image bytes at all: its figure output is a description
plus chart.js or mermaid, and the supported figure types are business charts
(bar, line, pie, radar, scatter, bubble, quadrant, mixed, flow, sequence, Gantt).
An electrical one-line is none of those.

So the drawing image has to come from the source PDF, at a resolution we choose,
independent of which engine did the segmentation. That is all this module does.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

DEFAULT_DPI = 300


def render_page(data: bytes, page_number: int, dpi: int = DEFAULT_DPI) -> bytes | None:
    """Render one 1-based PDF page to PNG bytes at ``dpi``."""
    try:
        import fitz
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


def render_pages(data: bytes, first: int, last: int, dpi: int = DEFAULT_DPI) -> dict[str, bytes]:
    """Render an inclusive page range, keyed ``page-<n>`` to match figure ids."""
    images: dict[str, bytes] = {}
    for page_number in range(first, last + 1):
        png = render_page(data, page_number, dpi)
        if png:
            images[f"page-{page_number}"] = png
    return images
