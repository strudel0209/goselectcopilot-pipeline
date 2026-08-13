from __future__ import annotations

import random

import pytest

from goselect_docproc.assemble import completion, document_order, merge, reassemble_markdown
from goselect_docproc.contracts import (
    ContentType,
    Evidence,
    ExtractionPayload,
    FileRef,
    Manifest,
    MotorSpec,
    Pair,
    Quantity,
    Region,
    Segment,
    SegmentResult,
    Span,
    Status,
    VfdSpec,
)


def _segment(seg_id, file_id, first, last, kind, offset, confidence=0.9):
    return Segment(
        segment_id=seg_id,
        file_id=file_id,
        first_page=first,
        last_page=last,
        content_type=kind,
        confidence=confidence,
        regions=[
            Region(kind=kind, ref="r", page=first, spans=[Span(offset=offset, length=10)])
        ],
    )


@pytest.fixture
def manifest():
    return Manifest(
        job_id="job-1",
        correlation_id="corr-1",
        files=[
            FileRef(file_id="f1", ordinal=0, source_uri="a", page_count=3,
                    content_sha256="x", content_chars=100),
            FileRef(file_id="f2", ordinal=1, source_uri="b", page_count=2,
                    content_sha256="y", content_chars=60),
        ],
        segments=[
            _segment("f1-seg-001", "f1", 1, 1, ContentType.TEXT, 0),
            _segment("f1-seg-002", "f1", 2, 2, ContentType.SCHEDULE, 40),
            _segment("f1-seg-003", "f1", 3, 3, ContentType.DRAWING, 80),
            _segment("f2-seg-001", "f2", 1, 1, ContentType.TEXT, 5),
        ],
    )


def _result(manifest, segment, status=Status.DONE, payload=None):
    return SegmentResult(
        id=f"{manifest.job_id}:{segment.file_id}:{segment.segment_id}",
        job_id=manifest.job_id,
        correlation_id=manifest.correlation_id,
        file_id=segment.file_id,
        file_ordinal=manifest.file(segment.file_id).ordinal,
        segment_id=segment.segment_id,
        content_type=segment.content_type,
        status=status,
        start_offset=segment.start,
        payload=payload,
    )


class TestOrdering:
    def test_completion_order_does_not_affect_document_order(self, manifest):
        results = [_result(manifest, s) for s in manifest.segments]
        expected = [r.segment_id for r in document_order(manifest, results)]

        for seed in range(20):
            shuffled = results[:]
            random.Random(seed).shuffle(shuffled)
            assert [r.segment_id for r in document_order(manifest, shuffled)] == expected

    def test_files_are_ordered_by_frozen_ordinal_not_page(self, manifest):
        results = [_result(manifest, s) for s in manifest.segments]
        ordered = document_order(manifest, results)
        assert [r.file_id for r in ordered] == ["f1", "f1", "f1", "f2"]

    def test_reassembly_is_stable_across_shuffles(self, manifest):
        content = {"f1": "".join(str(i % 10) for i in range(100)), "f2": "abcdefghij" * 6}
        first = reassemble_markdown(manifest, content)
        manifest.segments = list(reversed(manifest.segments))
        assert reassemble_markdown(manifest, content) == first


class TestCompletion:
    def test_duplicate_delivery_is_not_double_counted(self, manifest):
        segment = manifest.segments[0]
        results = [_result(manifest, segment), _result(manifest, segment)]
        expected, done, failed = completion(manifest, results)
        assert (expected, done, failed) == (4, 1, 0)

    def test_failure_is_counted_and_not_hidden(self, manifest):
        results = [_result(manifest, s) for s in manifest.segments[:3]]
        results.append(_result(manifest, manifest.segments[3], status=Status.FAILED))
        assert completion(manifest, results) == (4, 3, 1)


class TestMerge:
    def _payload(self, tag, power, origin, page=1):
        evidence = Evidence(file_id="f1", page=page, spans=[Span(offset=0, length=5)])
        return ExtractionPayload(
            motors=[MotorSpec(tag=tag, power=Quantity(value=power, unit="kW"), evidence=[evidence])],
            pairs=[
                Pair(pair_id=f"p-{origin.value}", vfd_tag="VFD-401", motor_tag=tag,
                     origin=origin, confidence=0.6, evidence=[evidence])
            ],
        )

    def test_same_tag_from_two_segments_merges_to_one_motor(self, manifest):
        results = [
            _result(manifest, manifest.segments[0],
                    payload=self._payload("M-401", 75.0, ContentType.TEXT)),
            _result(manifest, manifest.segments[1],
                    payload=self._payload("M-401", 75.0, ContentType.SCHEDULE)),
        ]
        job = merge(manifest, results)
        assert len(job.payload.motors) == 1
        assert len(job.payload.pairs) == 1

    def test_disagreement_is_recorded_not_silently_resolved(self, manifest):
        results = [
            _result(manifest, manifest.segments[0],
                    payload=self._payload("M-401", 75.0, ContentType.TEXT)),
            _result(manifest, manifest.segments[1],
                    payload=self._payload("M-401", 90.0, ContentType.SCHEDULE)),
        ]
        job = merge(manifest, results)
        assert job.conflicts
        assert "M-401.power" in {c.field for c in job.conflicts}

    def test_untagged_project_requirements_survive_the_merge(self, manifest):
        """A Division 16 clause names no equipment. Dropping untagged specs threw
        away the entire ABB requirement set on the Howey package."""
        evidence = Evidence(file_id="f1", page=1, spans=[Span(offset=0, length=5)])
        results = [
            _result(manifest, manifest.segments[0], payload=ExtractionPayload(
                vfds=[VfdSpec(tag=None, enclosure="NEMA 1", evidence=[evidence])])),
            _result(manifest, manifest.segments[1], payload=ExtractionPayload(
                vfds=[VfdSpec(tag=None, voltage=Quantity(value=460.0, unit="V"),
                              evidence=[evidence])])),
        ]
        job = merge(manifest, results)
        assert len(job.payload.vfds) == 1, "untagged requirements consolidate, not multiply"
        held = job.payload.vfds[0]
        assert held.tag is None
        assert held.enclosure == "NEMA 1"
        assert held.voltage.value == 460.0

    def test_conflicting_untagged_values_are_reported_against_project(self, manifest):
        evidence = Evidence(file_id="f1", page=1, spans=[Span(offset=0, length=5)])
        results = [
            _result(manifest, manifest.segments[0], payload=ExtractionPayload(
                vfds=[VfdSpec(tag=None, voltage=Quantity(value=460.0, unit="V"),
                              evidence=[evidence])])),
            _result(manifest, manifest.segments[1], payload=ExtractionPayload(
                vfds=[VfdSpec(tag=None, voltage=Quantity(value=100.0, unit="V"),
                              evidence=[evidence])])),
        ]
        job = merge(manifest, results)
        assert "<project>.voltage" in {c.field for c in job.conflicts}

    def test_schedule_wins_for_numeric_specs(self, manifest):
        results = [
            _result(manifest, manifest.segments[0],
                    payload=self._payload("M-401", 75.0, ContentType.TEXT)),
            _result(manifest, manifest.segments[1],
                    payload=self._payload("M-401", 90.0, ContentType.SCHEDULE)),
        ]
        job = merge(manifest, results)
        assert job.payload.motors[0].power.value == 90.0

    def test_corroboration_across_origins_raises_pair_confidence(self, manifest):
        results = [
            _result(manifest, manifest.segments[1],
                    payload=self._payload("M-401", 75.0, ContentType.SCHEDULE)),
            _result(manifest, manifest.segments[2],
                    payload=self._payload("M-401", 75.0, ContentType.DRAWING)),
        ]
        job = merge(manifest, results)
        assert job.payload.pairs[0].confidence > 0.6

    def test_partial_failure_yields_review_not_success(self, manifest):
        results = [_result(manifest, s) for s in manifest.segments[:3]]
        results.append(_result(manifest, manifest.segments[3], status=Status.FAILED))
        job = merge(manifest, results)
        assert job.status is Status.REVIEW
        assert job.partial
        assert "f2-seg-001" in job.review_required

    def test_low_confidence_segments_are_routed_to_review(self, manifest):
        manifest.segments[0].confidence = 0.05
        job = merge(manifest, [_result(manifest, s) for s in manifest.segments])
        assert "f1-seg-001" in job.review_required
