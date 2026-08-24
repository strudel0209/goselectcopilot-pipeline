"""Producer adapter tests.

The mapping from a service response to segments and regions is where producers
are most likely to be wrong, so each is tested against a fake response. No
credentials, no network, no spend.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from azure.ai.contentunderstanding.models import (
    AnalysisResult,
    ContentSpan,
    DocumentContent,
    DocumentContentSegment,
    DocumentFigure,
    DocumentPage,
    DocumentParagraph,
    DocumentSection,
    DocumentTable,
)

from goselect_docproc.contracts import ContentType
from goselect_docproc.producers import available
from goselect_docproc.producers.base import ProducerCapabilities, ProducerCost
from goselect_docproc.producers.content_understanding import (
    CATEGORY_TO_KIND,
    DOCUMENT_CATEGORIES,
    MAX_DESCRIPTION_CHARS,
    ContentUnderstandingProducer,
    router_analyzer,
)
from goselect_docproc.producers.di_layout import DILayoutProducer
from goselect_docproc.spans import overlaps


class FakeLayout:
    """Stands in for ``LayoutClient``: returns a canned result and a digest."""

    def __init__(self, result):
        self.result = result

    def analyze(self, data, options=None):
        return self.result, "deadbeef"


class TestDILayoutEndToEnd:
    """Exercises the real wiring. The adapter tests above use fakes for the
    service; this one uses a fake for the service and the real everything else,
    which is what catches import and signature breakage."""

    def test_analyze_produces_segments_and_a_section_index(self, stapled_package_result):
        analysis = DILayoutProducer(FakeLayout(stapled_package_result)).analyze(
            "f1", b"%PDF-", "file:///x.pdf"
        )
        assert analysis.segments
        assert analysis.content_sha256 == "deadbeef"
        assert analysis.section_index.strategy == "di-sections"

    def test_drawing_segments_never_carry_a_specification_clause(
        self, stapled_package_result
    ):
        analysis = DILayoutProducer(FakeLayout(stapled_package_result)).analyze(
            "f1", b"%PDF-", "file:///x.pdf"
        )
        for segment in analysis.segments:
            if segment.content_type is ContentType.DRAWING:
                assert segment.section_root != "SECTION 16370 VFD > 3.05 TESTS"


class FakeCU:
    """Returns the same ``AnalysisResult`` the SDK hands back, so a renamed
    field breaks a test instead of silently reading None."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def begin_analyze_binary(self, analyzer_id, binary_input, **kwargs):
        self.calls.append(("binary", analyzer_id, kwargs))
        return NS(result=lambda: self.result)

    def begin_analyze(self, analyzer_id, **kwargs):
        self.calls.append(("url", analyzer_id, kwargs))
        return NS(result=lambda: self.result)


class TestContentUnderstanding:
    """Shaped from a real response: contents[0] carries the markdown, the full
    layout model AND the classifier segments."""

    @pytest.fixture
    def document(self):
        #        0                    40                        80
        content = "SPEC HEADING" + "x" * 28 + "<table>rows</table>" + "y" * 21 + "FOOTER"
        return DocumentContent(
            markdown=content,
            pages=[
                DocumentPage(page_number=1, spans=[ContentSpan(offset=0, length=40)]),
                DocumentPage(page_number=2, spans=[ContentSpan(offset=40, length=46)]),
            ],
            paragraphs=[
                DocumentParagraph(role="sectionHeading", content="SPEC HEADING",
                                  span=ContentSpan(offset=0, length=12)),
                DocumentParagraph(role=None, content="x" * 28,
                                  span=ContentSpan(offset=12, length=28)),
                DocumentParagraph(role="pageFooter", content="FOOTER",
                                  span=ContentSpan(offset=80, length=6)),
            ],
            tables=[DocumentTable(row_count=2, column_count=2,
                                 span=ContentSpan(offset=40, length=19))],
            figures=[DocumentFigure(id="2.1", span=ContentSpan(offset=59, length=21))],
            segments=[
                DocumentContentSegment(segment_id="s1", category="text", confidence=1.0,
                                       start_page_number=1, end_page_number=1,
                                       span=ContentSpan(offset=0, length=40)),
                DocumentContentSegment(segment_id="s2", category="drawing", confidence=1.0,
                                       start_page_number=2, end_page_number=2,
                                       span=ContentSpan(offset=40, length=46)),
            ],
        )

    @pytest.fixture
    def producer(self, document):
        return ContentUnderstandingProducer(FakeCU(AnalysisResult(contents=[document])))

    def test_categories_map_to_content_types(self, producer):
        analysis = producer.analyze("f1", b"%PDF-fake")
        assert [s.content_type for s in analysis.segments] == [
            ContentType.TEXT,
            ContentType.DRAWING,
        ]

    def test_reads_contents_not_top_level(self, producer):
        """The payload is result.contents[]; a top-level read returns nothing."""
        analysis = producer.analyze("f1", b"%PDF-fake")
        assert analysis.content
        assert analysis.page_count == 2

    def test_segment_spans_come_from_the_classifier(self, producer):
        analysis = producer.analyze("f1", b"%PDF-fake")
        assert analysis.segments[0].start == 0
        assert analysis.segments[1].start >= 40

    def test_intra_page_regions_are_produced_from_the_layout_model(self, producer):
        """The classifier is page-level, but tables and figures carry spans, so
        span subtraction still applies."""
        analysis = producer.analyze("f1", b"%PDF-fake")
        kinds = {r.kind for s in analysis.segments for r in s.regions}
        assert ContentType.SCHEDULE in kinds
        assert ContentType.DRAWING in kinds
        assert ContentType.TEXT in kinds
        assert producer.capabilities.intra_page is True


    def test_furniture_is_declared_so_coverage_does_not_count_it_as_loss(self, producer):
        analysis = producer.analyze("f1", b"%PDF-fake")
        claimed = "".join(
            analysis.content[s.offset : s.offset + s.length]
            for seg in analysis.segments
            for r in seg.regions
            for s in r.spans
        )
        assert "FOOTER" not in claimed
        assert analysis.coverage().unexplained_chars == 0

    def test_regions_never_overlap(self, producer):
        analysis = producer.analyze("f1", b"%PDF-fake")
        spans = [s.as_tuple() for seg in analysis.segments for r in seg.regions for s in r.spans]
        for i, a in enumerate(spans):
            for b in spans[i + 1 :]:
                assert not overlaps([a], [b])

    def test_empty_analysis_scores_zero_coverage_not_one(self, producer):
        from goselect_docproc.contracts import Coverage

        assert Coverage(total_chars=0, covered_chars=0, furniture_chars=0,
                        unexplained_chars=0).accounted_ratio == 0.0


class TestContentUnderstandingSections:
    """``sections`` carries nesting depth and, at the root, the boundaries between
    documents stapled into one file. Heading roles carry neither."""

    def document(self, sections):
        content = "SECTION 16370 VFD\n3.05 TESTS\nMegger the terminals.\n2x 124 Amp\n"
        return DocumentContent(
            markdown=content,
            pages=[DocumentPage(page_number=1, spans=[ContentSpan(offset=0, length=len(content))])],
            paragraphs=[
                DocumentParagraph(role="title", content="SECTION 16370 VFD",
                                  span=ContentSpan(offset=0, length=17)),
                DocumentParagraph(role="sectionHeading", content="3.05 TESTS",
                                  span=ContentSpan(offset=18, length=10)),
                DocumentParagraph(role=None, content="Megger the terminals.",
                                  span=ContentSpan(offset=29, length=21)),
                DocumentParagraph(role="title", content="2x 124 Amp",
                                  span=ContentSpan(offset=51, length=10)),
            ],
            sections=sections,
            segments=[
                DocumentContentSegment(segment_id="s1", category="text", confidence=1.0,
                                       start_page_number=1, end_page_number=1,
                                       span=ContentSpan(offset=0, length=len(content))),
            ],
        )

    @property
    def stapled(self):
        """Root with two children: a specification, and a drawing sheet."""
        return [
            DocumentSection(elements=["/sections/1", "/sections/3"],
                            span=ContentSpan(offset=0, length=62)),
            DocumentSection(elements=["/paragraphs/0", "/sections/2"],
                            span=ContentSpan(offset=0, length=50)),
            DocumentSection(elements=["/paragraphs/1", "/paragraphs/2"],
                            span=ContentSpan(offset=18, length=32)),
            DocumentSection(elements=["/paragraphs/3"], span=ContentSpan(offset=51, length=11)),
        ]

    def analyse(self, sections):
        producer = ContentUnderstandingProducer(
            FakeCU(AnalysisResult(contents=[self.document(sections)]))
        )
        return producer.analyze("f1", b"%PDF-fake")

    def test_the_native_tree_is_preferred_over_heading_roles(self):
        index = self.analyse(self.stapled).section_index

        assert index.strategy == "content-understanding-sections"
        assert [n.heading for n in index.nodes] == [
            "SECTION 16370 VFD", "3.05 TESTS", "2x 124 Amp",
        ]

    def test_nesting_depth_comes_from_the_tree_not_a_guess(self):
        index = self.analyse(self.stapled).section_index

        assert {n.heading: n.level for n in index.nodes}["3.05 TESTS"] == 2

    def test_attribution_never_crosses_a_document_boundary(self):
        """The drawing sheet is a separate root child; it must not inherit the
        specification's clause."""
        index = self.analyse(self.stapled).section_index

        assert index.path_for(29) == "SECTION 16370 VFD > 3.05 TESTS"
        assert index.path_for(51) == "2x 124 Amp"

    def test_a_bare_root_emits_no_headings(self):
        """One entry means the service found no structure. Falling back to role
        scanning is how every label on a one-line diagram becomes a clause."""
        bare = [DocumentSection(elements=[], span=ContentSpan(offset=0, length=62))]

        index = self.analyse(bare).section_index

        assert index.strategy == "content-understanding-sections-flat"
        assert index.nodes == []

    def test_roles_are_the_fallback_when_no_tree_is_returned(self):
        index = self.analyse([]).section_index

        assert index.strategy == "content-understanding-roles"
        assert [n.heading for n in index.nodes] == [
            "SECTION 16370 VFD", "3.05 TESTS", "2x 124 Amp",
        ]


class TestRouterAnalyzerDefinition:
    """The analyzer is a typed resource now, so a wrong shape fails here rather
    than as a 400 from the service."""

    @pytest.fixture
    def analyzer(self):
        return router_analyzer()

    def test_base_analyzer_is_prebuilt_document(self, analyzer):
        assert analyzer.base_analyzer_id == "prebuilt-document"

    def test_segmentation_and_grounding_are_on(self, analyzer):
        assert analyzer.config.enable_segment is True
        assert analyzer.config.estimate_field_source_and_confidence is True
        assert analyzer.config.return_details is True

    def test_formula_detection_is_declared_off(self, analyzer):
        """The service default is on, and it reads a boxed tag as a radical."""
        assert analyzer.config.enable_formula is False

    def test_in_page_segments_is_opt_in(self):
        assert router_analyzer().config.allow_in_page_segments is False
        assert router_analyzer(in_page_segments=True).config.allow_in_page_segments is True

    def test_completion_model_is_declared(self, analyzer):
        assert analyzer.models["completion"]

    def test_catch_all_category_exists(self, analyzer):
        """Without it, content is forced into one of the real categories."""
        assert "other" in analyzer.config.content_categories

    def test_every_category_has_a_description(self, analyzer):
        categories = analyzer.config.content_categories
        assert all(c.description for c in categories.values())
        assert len(categories) <= 200

    @pytest.mark.parametrize("name", sorted(CATEGORY_TO_KIND))
    def test_category_description_fits_the_service_limit(self, name):
        assert len(DOCUMENT_CATEGORIES[name].description) <= MAX_DESCRIPTION_CHARS

    def test_declared_categories_and_routing_table_agree(self, analyzer):
        """A category the service can return but the map has no entry for is
        silently downgraded to OTHER, which looks like a classifier failure."""
        assert set(analyzer.config.content_categories) == set(CATEGORY_TO_KIND)

    def test_plan_is_a_category_of_its_own(self):
        """Plans are dropped, so they must be separable from drawings first."""
        assert CATEGORY_TO_KIND["plan"] is ContentType.PLAN
        assert CATEGORY_TO_KIND["drawing"] is ContentType.DRAWING


class TestRegistryAndCost:
    def test_both_producers_are_registered(self):
        assert set(available()) == {"di-layout", "content-understanding"}

    def test_costs_add_when_an_engine_calls_two_services(self):
        combined = ProducerCost(pages=10, api_calls=1, usd_estimate=0.10) + ProducerCost(
            pages=10, api_calls=1, usd_estimate=0.04
        )
        assert combined.api_calls == 2
        assert combined.usd_estimate == 0.14
        assert combined.usd_per_page == 0.007

    def test_capability_check_rejects_oversized_documents(self):
        capabilities = ProducerCapabilities(max_pages=30, preview=True)
        problems = capabilities.check(page_count=40, byte_count=1000)
        assert any("30-page" in p for p in problems)
        assert any("Preview" in p for p in problems)


class TestSheetsDoNotInheritProse:
    """A plan sheet has its own title block. Letting it inherit the last clause
    of the preceding specification is how a rating is traced to the wrong page."""

    def test_plan_and_drawing_are_both_self_titling(self):
        from goselect_docproc.sections import SELF_TITLING

        assert SELF_TITLING == {ContentType.DRAWING, ContentType.PLAN}
        assert ContentType.TEXT not in SELF_TITLING
        assert ContentType.SCHEDULE not in SELF_TITLING
