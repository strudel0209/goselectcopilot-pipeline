"""Producer adapter tests.

The mapping from a service response to segments and regions is where producers
are most likely to be wrong, so each is tested against a fake response. No
credentials, no network, no spend.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from goselect_docproc.contracts import ContentType
from goselect_docproc.producers import available
from goselect_docproc.producers.base import ProducerCapabilities, ProducerCost
from goselect_docproc.producers.content_understanding import ContentUnderstandingProducer
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
    def __init__(self, response):
        self.response = response

    def analyze(self, *, analyzer_id, data, source_uri=None):
        return self.response


class TestContentUnderstanding:
    """Shaped from a real 2025-11-01 response: result.contents[0] carries the
    markdown, the full layout model AND the classifier segments."""

    @pytest.fixture
    def producer(self):
        #        0                    40                        80
        content = "SPEC HEADING" + "x" * 28 + "<table>rows</table>" + "y" * 21 + "FOOTER"
        page_one = {"pageNumber": 1, "spans": [{"offset": 0, "length": 40}]}
        page_two = {"pageNumber": 2, "spans": [{"offset": 40, "length": 46}]}

        item = {
            "markdown": content,
            "pages": [page_one, page_two],
            "paragraphs": [
                {"role": "sectionHeading", "content": "SPEC HEADING", "span": {"offset": 0, "length": 12}},
                {"role": None, "content": "x" * 28, "span": {"offset": 12, "length": 28}},
                {"role": "pageFooter", "content": "FOOTER", "span": {"offset": 80, "length": 6}},
            ],
            "tables": [{"rowCount": 2, "columnCount": 2, "span": {"offset": 40, "length": 19}}],
            "figures": [{"id": "2.1", "span": {"offset": 59, "length": 21}}],
            "segments": [
                {"segmentId": "s1", "span": {"offset": 0, "length": 40},
                 "startPageNumber": 1, "endPageNumber": 1, "category": "TextSpecification"},
                {"segmentId": "s2", "span": {"offset": 40, "length": 46},
                 "startPageNumber": 2, "endPageNumber": 2, "category": "Drawing"},
            ],
        }
        return ContentUnderstandingProducer(FakeCU({"contents": [item]}))

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


class TestRouterAnalyzerDefinition:
    """Pinned to the documented 2025-11-01 contract; a wrong shape returns 400."""

    def test_base_analyzer_is_prebuilt_document(self):
        from goselect_docproc.producers.content_understanding import ROUTER_ANALYZER

        assert ROUTER_ANALYZER["baseAnalyzerId"] == "prebuilt-document"

    def test_content_categories_live_inside_config(self):
        from goselect_docproc.producers.content_understanding import ROUTER_ANALYZER

        assert "contentCategories" not in ROUTER_ANALYZER
        assert "contentCategories" in ROUTER_ANALYZER["config"]
        assert ROUTER_ANALYZER["config"]["enableSegment"] is True

    def test_completion_model_is_declared(self):
        from goselect_docproc.producers.content_understanding import ROUTER_ANALYZER

        assert ROUTER_ANALYZER["models"]["completion"]

    def test_catch_all_category_exists(self):
        """Without it, content is forced into one of the three real categories."""
        from goselect_docproc.producers.content_understanding import ROUTER_ANALYZER

        assert "Other" in ROUTER_ANALYZER["config"]["contentCategories"]

    def test_every_category_has_a_description(self):
        from goselect_docproc.producers.content_understanding import ROUTER_ANALYZER

        categories = ROUTER_ANALYZER["config"]["contentCategories"]
        assert all(c.get("description") for c in categories.values())
        assert len(categories) <= 200


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
