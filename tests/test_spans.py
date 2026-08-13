from __future__ import annotations

import pytest

from goselect_docproc.spans import (
    gaps,
    is_benign_gap,
    normalise,
    overlaps,
    subtract,
    text_for,
    total,
)


class TestSubtract:
    def test_hole_in_the_middle_splits(self):
        assert subtract([(0, 100)], [(30, 20)]) == [(0, 30), (50, 50)]

    def test_fully_consumed_returns_empty(self):
        assert subtract([(0, 100)], [(0, 100)]) == []

    def test_no_overlap_is_untouched(self):
        assert subtract([(0, 50)], [(60, 10)]) == [(0, 50)]

    def test_partial_head_and_tail(self):
        assert subtract([(10, 30)], [(0, 15), (35, 20)]) == [(15, 20)]

    def test_multiple_blocks(self):
        assert subtract([(0, 100)], [(10, 10), (40, 10), (80, 10)]) == [
            (0, 10),
            (20, 20),
            (50, 30),
            (90, 10),
        ]

    def test_adjacent_not_overlapping(self):
        assert subtract([(0, 10)], [(10, 5)]) == [(0, 10)]

    def test_claim_is_idempotent(self):
        once = subtract([(0, 100)], [(30, 20)])
        assert subtract(once, [(30, 20)]) == once


class TestOverlaps:
    @pytest.mark.parametrize(
        "a,b,expected",
        [
            ([(0, 10)], [(5, 10)], True),
            ([(0, 10)], [(10, 10)], False),
            ([(0, 10)], [(11, 1)], False),
            ([], [(0, 10)], False),
            ([(0, 1), (50, 1)], [(50, 1)], True),
        ],
    )
    def test_cases(self, a, b, expected):
        assert overlaps(a, b) is expected


class TestCoverageHelpers:
    def test_gaps_finds_uncovered_head_middle_tail(self):
        assert gaps(100, [(10, 10), (50, 10)]) == [(0, 10), (20, 30), (60, 40)]

    def test_no_gaps_when_fully_covered(self):
        assert gaps(50, [(0, 50)]) == []

    def test_total_deduplicates_overlaps(self):
        assert total([(0, 10), (5, 10)]) == 15

    def test_normalise_merges(self):
        assert normalise([(5, 5), (0, 5), (20, 1)]) == [(0, 10), (20, 1)]


class TestBenignGap:
    @pytest.mark.parametrize(
        "text",
        [
            '<!-- PageFooter="ABB" -->',
            "\n\n<!-- PageBreak -->\n\n<!-- PageNumber=\"12\" -->\n",
            "<figure>\n\n",
            "</figcaption>\n\n",
            "   \n\t ",
        ],
    )
    def test_furniture_and_markup_is_benign(self, text):
        assert is_benign_gap(text)

    @pytest.mark.parametrize(
        "text",
        ["Motors shall be IP55.", "<!-- PageFooter -->VFD-401", "3.2 Scope of supply"],
    )
    def test_real_content_is_not_benign(self, text):
        assert not is_benign_gap(text)


def test_text_for_returns_document_order_regardless_of_input_order():
    content = "abcdefghij"
    assert text_for(content, [(6, 2), (0, 2)], separator="|") == "ab|gh"
