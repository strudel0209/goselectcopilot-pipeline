import pytest
from jsonschema import ValidationError

from goselect_docproc.assemble import merge
from goselect_docproc.contracts import ContentType, Evidence, ExtractionPayload
from goselect_docproc.deliver import contract_payload, review_sheet
from goselect_docproc.validate import validate_payload
from test_assemble import package, record


def test_json_and_review_read_the_same_assembled_rows_without_mutation():
    system = record(**{"motor.electrical.power_rating": {"value": 12, "unit": "HP", "details": "12 HP"}})
    job = merge(*package((ContentType.SCHEDULE, [system])))
    before = job.model_dump_json()
    delivered = contract_payload(job)
    sheet = review_sheet(job)
    assert delivered["vfd_motor_pairs"] == [system.row]
    assert "12 HP" in sheet and "power_rating.value" not in sheet
    assert "schema population" not in sheet and "input_voltage" not in sheet
    assert job.model_dump_json() == before
    delivered["vfd_motor_pairs"][0]["tag"] = "changed"
    assert job.systems[0].row["tag"] == "drive-X"


def test_unassigned_requirements_are_reviewed_but_not_exported_as_equipment():
    job = merge(*package((ContentType.TEXT, [record(None, ContentType.TEXT, **{"vfd.manufacturer": "Vendor"})])))
    assert contract_payload(job) == {"vfd_motor_pairs": []}
    assert "Unassigned" in review_sheet(job) and "Vendor" in review_sheet(job)


def test_invalid_customer_rows_fail_export():
    job = merge(*package((ContentType.SCHEDULE, [record()])))
    job.systems[0].row = {"tag": "invalid"}
    with pytest.raises(ValidationError):
        contract_payload(job)


def test_validation_requires_field_evidence_and_keeps_low_confidence_visible():
    system = record()
    payload = ExtractionPayload(records=[system])
    assert any("tag: no native field evidence" in error for error in validate_payload(payload))
    system.issues.clear()
    system.evidence["tag"] = [Evidence(file_id="file", page=1, source="table", confidence=0.2, verbatim="drive-X")]
    assert not validate_payload(payload)
    assert any("confidence" in error for error in validate_payload(payload, field_review_threshold=0.8))