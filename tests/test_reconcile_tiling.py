from __future__ import annotations

import pytest

from goselect_docproc.tiling import (
    VisionLimits,
    assess,
    downscale_factor,
    plan_tiles,
    visual_tokens,
)


class TestVisionBudget:
    def test_token_formula_matches_published_patches(self):
        assert visual_tokens(1000, 1000) == 1296
        assert visual_tokens(200, 200) == 64

    @pytest.mark.parametrize(
        "size,tier,expected",
        [
            ((3840, 2160), VisionLimits.high_resolution(), (2576, 1449)),
            ((2000, 1500), VisionLimits.high_resolution(), (2000, 1500)),
            ((1000, 1000), VisionLimits.standard(), (1000, 1000)),
        ],
    )
    def test_downscale_matches_published_table(self, size, tier, expected):
        scale = downscale_factor(*size, tier)
        assert (int(size[0] * scale), int(size[1] * scale)) == expected

    def test_e_size_drawing_is_illegible_whole_on_standard_tier(self):
        """13200x10200 is a 44x34in sheet at 300 DPI - their CAD case."""
        whole = assess(13200, 10200, VisionLimits.standard())
        assert not whole.readable
        assert whole.effective_text_px < 6

    def test_tiling_restores_legibility(self):
        tiles = plan_tiles(13200, 10200, VisionLimits.high_resolution())
        box = tiles[0]
        tile = assess(box[2] - box[0], box[3] - box[1], VisionLimits.high_resolution())
        assert tile.scale == 1.0
        assert tile.readable

    def test_small_images_are_not_tiled(self):
        assert plan_tiles(800, 600, VisionLimits.standard()) == [(0, 0, 800, 600)]

    def test_tiles_cover_the_whole_sheet(self):
        width, height = 5000, 4000
        tiles = plan_tiles(width, height, VisionLimits.high_resolution())
        assert min(t[0] for t in tiles) == 0
        assert min(t[1] for t in tiles) == 0
        assert max(t[2] for t in tiles) == width
        assert max(t[3] for t in tiles) == height

    def test_tiles_overlap_so_boundary_tags_survive(self):
        tiles = plan_tiles(5000, 1000, VisionLimits.high_resolution(), overlap=0.12)
        rows = sorted({(t[0], t[2]) for t in tiles})
        assert any(rows[i][1] > rows[i + 1][0] for i in range(len(rows) - 1))
