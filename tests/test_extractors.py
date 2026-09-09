"""Extraction routing.

The only branch with no model behind it is the one that must be provable: a
dropped content type has to look different from a type that was extracted and
found nothing.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock

from azure.ai.contentunderstanding.models import ArrayField, BooleanField, ObjectField, StringField

from goselect_docproc.contracts import ContentType, Region, Segment, Span, WorkItem
from goselect_docproc.extractors import (
    DROP_REASONS,
    DropExtractor,
    ModelExtractor,
    NullModel,
    SegmentContext,
    default_extractors,
)
from goselect_docproc.pipeline import Pipeline, PipelineConfig
from goselect_docproc.producers.base import DocumentAnalysis
from goselect_docproc.sections import SectionIndex
from goselect_docproc.validate import validate_payload

# A drive on every page the model is asked about, so "nothing came back" can only
# mean the segment was dropped.
CANNED = {
    "items": [{"kind": "VFD", "tag": "VFD-401", "power_raw": "75 kW", "source_text": "VFD-401"}],
    "notes": [],
}


def work_item(content_type: ContentType, segment_id: str = "f1-seg-004") -> WorkItem:
    return WorkItem(
        job_id="j1",
        correlation_id="c1",
        file_id="f1",
        file_ordinal=0,
        segment_id=segment_id,
        content_type=content_type,
        first_page=3,
        last_page=3,
        spans=[Span(offset=0, length=10)],
        layout_uri="jobs/j1/files/f1/layout.json",
    )


class TestDroppedContentTypes:
    def test_plan_is_routed_to_a_drop_extractor(self):
        extractors = default_extractors(NullModel())

        assert isinstance(extractors[ContentType.PLAN], DropExtractor)
        assert isinstance(extractors[ContentType.DRAWING], ModelExtractor)

    def test_dropping_a_plan_costs_no_model_call(self):
        model = Mock()
        extractors = default_extractors(model)
        context = SegmentContext(content="0123456789", item=work_item(ContentType.PLAN))

        extractors[ContentType.PLAN].extract(context)

        model.complete_json.assert_not_called()

    def test_a_dropped_segment_says_so_instead_of_looking_empty(self):
        context = SegmentContext(content="0123456789", item=work_item(ContentType.PLAN))

        payload = default_extractors(NullModel())[ContentType.PLAN].extract(context)

        assert not payload.records
        assert any("not extracted" in note for note in payload.notes)
        assert any("f1-seg-004" in note for note in payload.notes)

    def test_a_dropped_segment_is_not_routed_to_review(self):
        """Nothing was extracted, so there is nothing for a human to correct."""
        context = SegmentContext(content="0123456789", item=work_item(ContentType.PLAN))

        payload = default_extractors(NullModel())[ContentType.PLAN].extract(context)

        assert validate_payload(payload, require_grounding=True) == []

    def test_every_dropped_type_records_why(self):
        assert all(reason for reason in DROP_REASONS.values())


#  0        22          47        59
#  |  prose  |   plan    | schedule |
CONTENT = "Drives shall be 460 V" + "PLAN E-01 scale 1:50 dims" + "| Tag | HP |"


def region(kind: ContentType, page: int, offset: int, length: int) -> Region:
    return Region(kind=kind, ref=f"p{page}:{kind.value}", page=page,
                  spans=[Span(offset=offset, length=length)])


def segment(sid: str, page: int, content_type: ContentType, regions: list[Region]) -> Segment:
    return Segment(segment_id=sid, file_id="f1", first_page=page, last_page=page,
                   content_type=content_type, confidence=0.99, regions=regions)


class FakeProducer:
    name = "fake"

    def __init__(self, segments):
        self.segments = segments

    def analyze(self, file_id, data, source_uri=None):
        return DocumentAnalysis(
            file_id=file_id, content=CONTENT, page_count=3, content_sha256="x",
            segments=self.segments, producer=self.name,
            section_index=SectionIndex(nodes=[], strategy="none", role_headings=0),
            fields_by_segment={segment.segment_id: {"rows": ArrayField(value_array=[ObjectField(value_object={
                "tag": StringField(value_string="drive-X", source=f"D({segment.first_page},0,0,1,0,1,1,0,1)"),
                "motor_tag": StringField(value_string="motor-X", source=f"D({segment.first_page},0,0,1,0,1,1,0,1)"),
                "identified_system": BooleanField(value_boolean=segment.content_type is ContentType.SCHEDULE),
            })])} for segment in self.segments if segment.content_type is not ContentType.PLAN},
        )

    def figure_image(self, analysis, figure_id):
        return None


def build(segments):
    model = Mock()
    pipeline = Pipeline(
        producer=FakeProducer(segments),
        extractors=default_extractors(model),
        config=PipelineConfig(max_workers=1),
    )
    return pipeline, model


class TestDroppingIsPerObjectNotPerFile:
    """A plan inside a package must not take the package - or its own page - down
    with it."""

    def segments(self):
        return [
            segment("f1-seg-001", 1, ContentType.TEXT, [region(ContentType.TEXT, 1, 0, 21)]),
            segment("f1-seg-002", 2, ContentType.PLAN, [region(ContentType.DRAWING, 2, 21, 25)]),
            segment("f1-seg-003", 3, ContentType.SCHEDULE, [region(ContentType.SCHEDULE, 3, 46, 12)]),
        ]

    def test_only_the_plan_segment_yields_nothing(self):
        pipeline, _ = build(self.segments())

        manifest, job, results = pipeline.run({"f1": (b"%PDF-", "file:///a.pdf")})
        by_id = {r.segment_id: r for r in results}

        assert by_id["f1-seg-002"].payload.records == []
        assert by_id["f1-seg-001"].payload.records
        assert by_id["f1-seg-003"].payload.records
        assert job.systems, "the file must survive its plan sheet"

    def test_the_dropped_segment_still_completes_the_job(self):
        pipeline, _ = build(self.segments())

        manifest, job, results = pipeline.run({"f1": (b"%PDF-", "file:///a.pdf")})

        assert job.segments_done == job.segments_expected == 3
        assert job.segments_failed == 0

    def test_dropping_does_not_break_the_coverage_proof(self):
        pipeline, _ = build(self.segments())

        manifest = pipeline.segment({"f1": (b"%PDF-", "file:///a.pdf")})

        assert manifest.coverage["f1"].unexplained_chars == 0

    def test_a_plan_page_costs_no_model_call(self):
        pipeline, model = build(self.segments())

        pipeline.run({"f1": (b"%PDF-", "file:///a.pdf")})

        model.complete_json.assert_not_called()


class TestCollateralDrops:
    """Page-level segmentation cannot separate a schedule printed on a plan
    sheet. Dropping the page drops the schedule, which must not be silent."""

    def test_a_schedule_sharing_a_plan_page_is_reported(self, caplog):
        segments = [
            segment("f1-seg-001", 1, ContentType.PLAN, [
                region(ContentType.DRAWING, 1, 0, 46),
                region(ContentType.SCHEDULE, 1, 46, 12),
            ])
        ]
        pipeline, _ = build(segments)

        with caplog.at_level(logging.WARNING):
            pipeline.segment({"f1": (b"%PDF-", "file:///a.pdf")})

        assert "takes SCHEDULE regions with it" in caplog.text

    def test_a_plans_own_figure_is_not_reported_as_collateral(self, caplog):
        segments = [
            segment("f1-seg-001", 1, ContentType.PLAN, [region(ContentType.DRAWING, 1, 0, 58)])
        ]
        pipeline, _ = build(segments)

        with caplog.at_level(logging.WARNING):
            pipeline.segment({"f1": (b"%PDF-", "file:///a.pdf")})

        assert "takes" not in caplog.text
