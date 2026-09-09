import pytest

from goselect_docproc.assemble import merge, merge_records
from goselect_docproc.contracts import (
    ContentType, Evidence, ExtractionPayload, ExtractionRecord, FileRef, Manifest, Region, Segment, SegmentResult, Span, Status,
)
from goselect_docproc.field_schema import empty_contract_row, set_field


def record(tag="drive-X", origin=ContentType.SCHEDULE, **values):
    result = ExtractionRecord(row=empty_contract_row(), identified_system=bool(tag), motor_tag="motor-X", origin=origin)
    result.row["tag"] = tag
    for path, value in values.items():
        set_field(result.row, path, value)
    return result


def package(*payloads):
    segments, results = [], []
    for index, (kind, records) in enumerate(payloads):
        segment_id = f"segment-{index}"
        segments.append(Segment(segment_id=segment_id, file_id="file", first_page=1, last_page=1,
                                content_type=kind, confidence=0.99, regions=[Region(kind=kind, ref=segment_id, page=1, spans=[Span(offset=index, length=1)])]))
        results.append(SegmentResult(id=segment_id, job_id="job", correlation_id="correlation", file_id="file", file_ordinal=0,
                                     segment_id=segment_id, content_type=kind, start_offset=index, status=Status.DONE,
                                     payload=ExtractionPayload(records=records)))
    manifest = Manifest(job_id="job", correlation_id="correlation", files=[FileRef(file_id="file", ordinal=0, source_uri="local", page_count=1, content_sha256="digest", content_chars=len(segments))], segments=segments)
    return manifest, results


def test_merge_once_by_source_precedence_and_preserve_evidence():
    low = record(origin=ContentType.DRAWING, **{"vfd.manufacturer": "drawing vendor"})
    high = record(**{"vfd.manufacturer": "schedule vendor"})
    high.evidence["vfd.manufacturer"] = [Evidence(file_id="file", page=1, source="table", verbatim="schedule vendor")]
    manifest, results = package((ContentType.DRAWING, [low]), (ContentType.SCHEDULE, [high]))
    job = merge(manifest, list(reversed(results)))
    assert len(job.systems) == 1
    assert job.systems[0].row["vfd"]["manufacturer"] == "schedule vendor"
    assert job.systems[0].evidence == high.evidence
    assert len(job.conflicts) == 1 and job.status is Status.REVIEW
    assert low.row["vfd"]["manufacturer"] == "drawing vendor"


def test_unidentified_observations_never_become_equipment():
    observation = record("generic")
    observation.identified_system = False
    job = merge(*package((ContentType.DRAWING, [observation, record()])))
    assert [system.row["tag"] for system in job.systems] == ["drive-X"]
    assert len(job.unassigned) == 1


@pytest.mark.parametrize("valid", [True, False])
def test_only_grounded_approved_scope_propagates(valid):
    requirement = record(None, ContentType.TEXT, **{"vfd.manufacturer": "vendor", "motor.manufacturer": "motor vendor"})
    requirement.scopes["vfd.manufacturer"] = Evidence(file_id="file" if valid else "foreign", page=1, source="text", verbatim="All drives require vendor")
    manifest, results = package((ContentType.TEXT, [requirement]), (ContentType.DRAWING, [record()]))
    job = merge(manifest, results)
    assert (job.systems[0].row["vfd"]["manufacturer"] == "vendor") is valid
    assert job.systems[0].row["motor"]["manufacturer"] is None
    assert job.unassigned[0].row["motor"]["manufacturer"] == "motor vendor"
    assert requirement.row["vfd"]["manufacturer"] == "vendor"


def test_same_schedule_rows_and_different_endpoints_stay_distinct():
    job = merge(*package((ContentType.SCHEDULE, [record(), record()])))
    assert len(job.systems) == 2
    other = record(origin=ContentType.DRAWING)
    other.motor_tag = "different-motor"
    job = merge(*package((ContentType.DRAWING, [record(), other])))
    assert len(job.systems) == 2


def test_wrong_job_is_rejected_and_missing_results_are_reviewed():
    manifest, results = package((ContentType.SCHEDULE, [record()]))
    results[0].job_id = "another-job"
    with pytest.raises(ValueError, match="manifest"):
        merge(manifest, results)
    assert merge(manifest, []).status is Status.FAILED


def test_measurements_remain_atomic():
    conflicts = []
    first = record(**{"motor.electrical.power_rating": {"value": 10, "unit": "HP", "details": "10 HP"}})
    second = record(origin=ContentType.DRAWING, **{"motor.electrical.power_rating": {"value": 10, "unit": "kW", "details": "10 kW"}})
    merged = merge_records([second, first], conflicts)
    assert merged.row["motor"]["electrical"]["power_rating"] == first.row["motor"]["electrical"]["power_rating"]
    assert len(conflicts) == 1