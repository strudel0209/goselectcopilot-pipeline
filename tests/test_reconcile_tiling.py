from __future__ import annotations

import pytest

from goselect_docproc.reconcile import TagLexicon, harvest, normalise, signature
from goselect_docproc.tiling import (
    VisionLimits,
    assess,
    downscale_factor,
    plan_tiles,
    visual_tokens,
)


class TestNormalise:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (r"$\sqrt{150-401}$", "150-401"),
            (r"$\sqrt{15D-721}$", "15D-721"),
            ("VFD-401", "VFD-401"),
            (" vfd-401 ", "VFD-401"),
            ("V F D - 4 0 1", "VFD-401"),
        ],
    )
    def test_strips_latex_and_layout_noise(self, raw, expected):
        assert normalise(raw) == expected


class TestConfusionSignature:
    def test_observed_abb_corruption_shares_a_signature(self):
        assert signature("VFD-401") == signature("150-401")

    def test_distinct_tags_do_not_collide(self):
        assert signature("VFD-401") != signature("VFD-402")


class TestLexicon:
    @pytest.fixture
    def lexicon(self):
        return TagLexicon({"VFD-401", "VFD-721", "VFD-711", "VFD-821", "M-401"})

    def test_harvest_finds_tags_in_schedule_text(self):
        found = harvest(["| Tag | kW |", "| VFD-401 | 75 |", "| M-401 | 75 |"])
        assert found == {"VFD-401", "M-401"}

    def test_harvest_finds_letter_suffixed_tags_from_the_howey_oneline(self):
        found = harvest(["VFD-H1", "VFD-H3", "VFD-J4", "SWBD-1", "WELL NO.5"])
        assert {"VFD-H1", "VFD-H3", "VFD-J4"} <= found

    def test_harvest_still_rejects_numeric_ranges(self):
        assert harvest(["rated 10-20 A, 100-200 V"]) == set()

    def test_exact_match_is_untouched(self, lexicon):
        repair = lexicon.snap("VFD-401")
        assert repair.value == "VFD-401" and repair.method == "exact"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (r"$\sqrt{150-401}$", "VFD-401"),
            (r"$\sqrt{150-711}$", "VFD-711"),
            (r"$\sqrt{15D-721}$", "VFD-721"),
        ],
    )
    def test_repairs_the_exact_corruptions_seen_on_the_abb_drawing(self, lexicon, raw, expected):
        repair = lexicon.snap(raw)
        assert repair.value == expected
        assert repair.method == "confusion-class"

    def test_unknown_tag_is_left_alone_not_snapped_to_a_neighbour(self, lexicon):
        """VFD-101 scores ~86%% against VFD-401. Rewriting it would be data loss."""
        repair = lexicon.snap("VFD-101")
        assert repair.value == "VFD-101"
        assert repair.method in {"unchanged", "ambiguous"}

    def test_ambiguity_is_reported_rather_than_guessed(self):
        lexicon = TagLexicon({"VFD-401", "1FD-401"})
        repair = lexicon.snap(r"$\sqrt{150-401}$")
        assert repair.method == "ambiguous"
        assert repair.confidence == 0.0
        assert len(repair.ambiguous_with) == 2

    def test_empty_lexicon_never_invents_a_tag(self):
        repair = TagLexicon(set()).snap("VFD-401")
        assert repair.value == "VFD-401" and repair.confidence == 0.0


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
