from io import BytesIO

import pymupdf
import pytest
from PIL import Image

from goselect_docproc.render import DEFAULT_DPI, render_pages
from goselect_docproc.pipeline import PipelineConfig


def test_pipeline_uses_the_render_default_and_accepts_an_override():
    assert PipelineConfig().drawing_dpi == DEFAULT_DPI
    assert PipelineConfig(drawing_dpi=200).drawing_dpi == 200


@pytest.mark.parametrize("rotation", [0, 90])
def test_source_crop_excludes_adjacent_content(rotation):
    with pymupdf.open() as document:
        page = document.new_page(width=144, height=144)
        page.draw_rect(pymupdf.Rect(0, 0, 72, 144), color=None, fill=(1, 0, 0))
        page.draw_rect(pymupdf.Rect(72, 0, 144, 144), color=None, fill=(0, 0, 1))
        page.set_rotation(rotation)
        data = document.tobytes()
    source = "D(1,0,0,1,0,1,2,0,2)" if not rotation else "D(1,0,0,2,0,2,1,0,1)"
    images = render_pages(data, 1, 1, dpi=72, source=source, unit="inch")
    image = Image.open(BytesIO(next(iter(images.values()))))
    assert image.size == ((72, 144) if not rotation else (144, 72))
    assert image.getpixel((30, 30)) == (255, 0, 0)


def test_bad_source_never_falls_back_to_a_full_page():
    with pymupdf.open() as document:
        document.new_page()
        data = document.tobytes()
    with pytest.raises(ValueError, match="source page"):
        render_pages(data, 1, 1, source="D(invalid)", unit="inch")