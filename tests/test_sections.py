"""Section attribution, tested against the producer-neutral tree walk.

These assertions used to run through a Document Intelligence result object. The
policy they encode has nothing to do with that service, so they now exercise
``tree_index`` on the two neutral shapes a producer must reduce to: a paragraph
is ``(offset, page, content)`` and a section is ``(span, elements)``.
"""

from __future__ import annotations

from goselect_docproc.sections import SectionIndex, tree_index

# A stapled package: a specification with one nested clause, followed by an
# independent drawing sheet. Offsets are absolute into the one content string.
PARAGRAPHS = [
    (0, 1, "SECTION 16370 VFD"),
    (20, 1, "3.05 TESTS"),
    (51, 2, "2x 124 Amp"),
]
SECTIONS = [
    (None, ["/sections/1", "/sections/3"]),  # root: two independent documents
    ((0, 51), ["/paragraphs/0", "/sections/2"]),  # the specification
    ((20, 31), ["/paragraphs/1"]),  # a clause inside it
    ((51, 12), ["/paragraphs/2"]),  # the drawing sheet
]


def index_of(sections, paragraphs) -> SectionIndex:
    nodes, boundaries = tree_index(sections, paragraphs)
    return SectionIndex(
        nodes=nodes,
        strategy="tree",
        role_headings=len(nodes),
        boundaries=tuple(boundaries),
    )


class TestSectionTree:
    def test_nesting_depth_comes_from_the_tree_not_from_counting_dots(self):
        index = index_of(SECTIONS, PARAGRAPHS)
        levels = {n.heading: n.level for n in index.nodes}
        assert levels["SECTION 16370 VFD"] == 1
        assert levels["3.05 TESTS"] == 2
        assert levels["2x 124 Amp"] == 1  # a sibling document, not a sub-clause

    def test_each_root_subtree_becomes_a_boundary(self):
        index = index_of(SECTIONS, PARAGRAPHS)
        assert index.boundaries == ((0, 51), (51, 63))

    def test_breadcrumbs_resolve_within_the_specification(self):
        index = index_of(SECTIONS, PARAGRAPHS)
        assert index.path_for(35) == "SECTION 16370 VFD > 3.05 TESTS"

    def test_attribution_never_crosses_into_the_preceding_document(self):
        index = index_of(SECTIONS, PARAGRAPHS)
        assert index.path_for(55) == "2x 124 Amp"

    def test_a_bare_root_yields_no_headings(self):
        """A one-line diagram has no structure; inventing headings is how
        handwriting and equipment labels become clauses."""
        nodes, boundaries = tree_index(
            [(None, ["/paragraphs/0"])], [(0, 1, "3 x 124 Amp")]
        )
        assert nodes == []
        assert boundaries == []


class TestSelfTitlingContent:
    def test_a_drawing_does_not_inherit_the_clause_before_it(self):
        """A drawing segment starting mid-specification must refuse the
        breadcrumb: the sheet carries its own title block."""
        index = index_of(SECTIONS, PARAGRAPHS)
        assert index.root_for(40, inherits=True) == "SECTION 16370 VFD > 3.05 TESTS"
        assert index.root_for(40, inherits=False) is None

    def test_a_drawing_keeps_a_heading_that_is_its_own(self):
        index = index_of(SECTIONS, PARAGRAPHS)
        assert index.root_for(51, inherits=False) == "2x 124 Amp"


class TestHeadingHygiene:
    def test_bullets_are_never_promoted_verbatim(self):
        nodes, _ = tree_index(
            [(None, ["/sections/1"]), ((0, 20), ["/paragraphs/0"])],
            [(0, 1, "- Derating factors")],
        )
        assert [n.heading for n in nodes] == ["Derating factors"]

    def test_a_decorative_rule_is_not_a_heading(self):
        nodes, _ = tree_index(
            [(None, ["/sections/1"]), ((0, 3), ["/paragraphs/0"])],
            [(0, 1, "\u2014")],
        )
        assert nodes == []

    def test_a_paragraph_of_prose_is_not_a_heading(self):
        nodes, _ = tree_index(
            [(None, ["/sections/1"]), ((0, 400), ["/paragraphs/0"])],
            [(0, 1, "x" * 400)],
        )
        assert nodes == []
