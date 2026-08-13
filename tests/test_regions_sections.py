from __future__ import annotations

from goselect_docproc.contracts import ContentType
from goselect_docproc.regions import page_regions
from goselect_docproc.sections import build_section_index
from goselect_docproc.spans import overlaps


class TestSpanSubtraction:
    def test_table_cells_do_not_leak_into_narrative(self, mixed_page_result):
        regions = page_regions(mixed_page_result, 1, ContentType.SCHEDULE)
        text = next(r for r in regions if r.kind is ContentType.TEXT)
        table = next(r for r in regions if r.kind is ContentType.SCHEDULE)
        assert not overlaps(
            [s.as_tuple() for s in text.spans], [s.as_tuple() for s in table.spans]
        )

    def test_page_furniture_is_excluded(self, mixed_page_result):
        regions = page_regions(mixed_page_result, 1, ContentType.SCHEDULE)
        text = next(r for r in regions if r.kind is ContentType.TEXT)
        assert all(s.offset < 114 for s in text.spans)

    def test_all_three_kinds_recovered(self, mixed_page_result):
        kinds = {r.kind for r in page_regions(mixed_page_result, 1, ContentType.SCHEDULE)}
        assert kinds == {ContentType.TEXT, ContentType.SCHEDULE, ContentType.DRAWING}

    def test_regions_are_returned_in_document_order(self, mixed_page_result):
        starts = [r.start for r in page_regions(mixed_page_result, 1, ContentType.SCHEDULE)]
        assert starts == sorted(starts)


class TestClaimOrderByContentType:
    def test_drawing_absorbs_its_title_block(self, drawing_sheet_result):
        regions = page_regions(drawing_sheet_result, 1, ContentType.DRAWING)
        assert [r.kind for r in regions] == [ContentType.DRAWING]
        drawing = regions[0]
        assert drawing.absorbed_tables == [0]
        assert sum(s.length for s in drawing.spans) == 48  # figure + title block

    def test_schedule_keeps_the_table_separate(self, drawing_sheet_result):
        regions = page_regions(drawing_sheet_result, 1, ContentType.SCHEDULE)
        kinds = [r.kind for r in regions]
        assert ContentType.SCHEDULE in kinds and ContentType.DRAWING in kinds

    def test_claim_order_never_duplicates_content(self, drawing_sheet_result):
        for segment_type in (ContentType.DRAWING, ContentType.SCHEDULE):
            regions = page_regions(drawing_sheet_result, 1, segment_type)
            spans = [s.as_tuple() for r in regions for s in r.spans]
            for i, a in enumerate(spans):
                for b in spans[i + 1 :]:
                    assert not overlaps([a], [b])

    def test_small_figures_are_ignored_as_logos(self, mixed_page_result):
        regions = page_regions(
            mixed_page_result, 1, ContentType.SCHEDULE, min_figure_area_ratio=0.99
        )
        assert ContentType.DRAWING not in {r.kind for r in regions}


class TestSectionIndex:
    def test_uses_roles_when_available(self, mixed_page_result):
        index = build_section_index(mixed_page_result, min_role_headings=1)
        assert index.strategy == "roles"
        assert index.nodes[0].heading == "## Mechanical features"

    def test_breadcrumb_is_the_last_preceding_heading(self, mixed_page_result):
        index = build_section_index(mixed_page_result, min_role_headings=1)
        assert index.path_for(60) == "## Mechanical features"

    def test_offset_before_any_heading_has_no_path(self, mixed_page_result):
        index = build_section_index(mixed_page_result, min_role_headings=99)
        assert index.path_for(0) is None or index.strategy == "geometry-fallback"

    def test_falls_back_to_geometry_when_roles_are_missing(self, drawing_sheet_result):
        index = build_section_index(drawing_sheet_result)
        assert index.strategy == "geometry-fallback"
        assert index.reliable is False

    def test_bullets_are_never_promoted_to_headings(self):
        from tests.conftest import analyze_result, page, paragraph, poly

        result = analyze_result(
            content="x" * 200,
            pages=[page(1)],
            paragraphs=[
                paragraph(0, 20, 1, "- Derating factors", polygon=poly(1, 1, 5, 1.6)),
                paragraph(30, 20, 1, "3.2 Scope of supply", polygon=poly(1, 2, 5, 2.6)),
            ],
        )
        index = build_section_index(result)
        assert all(not n.heading.startswith("-") for n in index.nodes)


class TestDiSectionTree:
    """Document Intelligence's own ``sections`` tree, and the boundary it encodes."""

    def test_di_tree_is_preferred_over_role_scanning(self, stapled_package_result):
        index = build_section_index(stapled_package_result)
        assert index.strategy == "di-sections"
        assert index.reliable is True

    def test_nesting_depth_comes_from_the_tree_not_from_counting_dots(
        self, stapled_package_result
    ):
        index = build_section_index(stapled_package_result)
        levels = {n.heading: n.level for n in index.nodes}
        assert levels["SECTION 16370 VFD"] == 1
        assert levels["3.05 TESTS"] == 2
        assert levels["2x 124 Amp"] == 1  # a sibling document, not a sub-clause

    def test_each_root_subtree_becomes_a_boundary(self, stapled_package_result):
        index = build_section_index(stapled_package_result)
        assert index.boundaries == ((0, 50), (51, 62))

    def test_breadcrumbs_resolve_within_the_specification(self, stapled_package_result):
        index = build_section_index(stapled_package_result)
        assert index.path_for(35) == "SECTION 16370 VFD > 3.05 TESTS"

    def test_attribution_never_crosses_into_the_preceding_document(
        self, stapled_package_result
    ):
        index = build_section_index(stapled_package_result)
        assert index.path_for(55) == "2x 124 Amp"

    def test_a_drawing_does_not_inherit_the_clause_before_it(self, stapled_package_result):
        """The Howey defect: DI folded one sheet into clause 3.05, so a drawing
        segment starting mid-specification must still refuse the breadcrumb."""
        index = build_section_index(stapled_package_result)
        assert index.root_for(40, inherits=True) == "SECTION 16370 VFD > 3.05 TESTS"
        assert index.root_for(40, inherits=False) is None

    def test_a_drawing_keeps_a_heading_that_is_its_own(self, stapled_package_result):
        index = build_section_index(stapled_package_result)
        assert index.root_for(51, inherits=False) == "2x 124 Amp"

    def test_a_bare_root_yields_no_headings(self):
        """A one-line diagram has no structure; inventing headings is how
        handwriting and equipment labels become clauses."""
        from tests.conftest import analyze_result, di_section, page, paragraph

        result = analyze_result(
            content="3 x 124 Amp",
            pages=[page(1)],
            paragraphs=[paragraph(0, 11, 1, "3 x 124 Amp", role="title")],
            sections=[di_section(["/paragraphs/0"], 0, 11)],
        )
        index = build_section_index(result)
        assert index.strategy == "di-sections-flat"
        assert index.nodes == []
        assert index.path_for(5) is None
