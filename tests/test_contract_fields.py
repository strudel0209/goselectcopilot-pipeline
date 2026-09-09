import pytest
from azure.ai.contentunderstanding.models import ArrayField, BooleanField, NumberField, ObjectField, StringField

from goselect_docproc.contracts import ContentType, Span, WorkItem
from goselect_docproc.extractors import RoutedFieldExtractor, SegmentContext


def context(kind=ContentType.SCHEDULE, fields=None, text="source"):
    return SegmentContext(content=text, fields=fields, item=WorkItem(
        job_id="job", correlation_id="correlation", file_id="file", file_ordinal=0,
        segment_id="segment", content_type=kind, first_page=1, last_page=1,
        spans=[Span(offset=0, length=len(text))], layout_uri="local",
    ))


def extract(values, kind=ContentType.SCHEDULE, text="source"):
    fields = {"rows": ArrayField(value_array=[ObjectField(value_object=values)])}
    return RoutedFieldExtractor(kind).extract(context(kind, fields, text)).records[0]


def test_native_fields_map_without_cross_equipment_inference():
    record = extract({
        "tag": StringField(value_string="drive-X", source="D(1,0,0,1,0,1,1,0,1)"),
        "identified_system": BooleanField(value_boolean=True),
        "motor__electrical__service_factor__value": NumberField(value_number=1.15, source="D(1,0,0,1,0,1,1,0,1)"),
        "vfd__electrical__input_voltage__value": StringField(value_string="230/460"),
        "vfd__features": ArrayField(value_array=[StringField(value_string="stated feature")]),
    })
    assert record.row["motor"]["electrical"]["service_factor"]["value"] == 1.15
    assert record.row["vfd"]["electrical"]["input_voltage"]["value"] == "230/460"
    assert record.row["motor"]["electrical"]["voltage"]["value"] is None
    assert record.row["vfd"]["features"] == ["stated feature"]
    assert record.evidence["tag"][0].page == 1
    assert record.evidence["motor.electrical.service_factor.value"][0].page == 1


@pytest.mark.parametrize("quote, grounded", [("All project drives require vendor X.", True), ("Invented clause", False)])
def test_scope_requires_native_source_and_verbatim_context(quote, grounded):
    record = extract({
        "vfd__manufacturer": StringField(value_string="X"),
        "all_vfds_scope": ArrayField(value_array=[ObjectField(value_object={
            "field": StringField(value_string="vfd.manufacturer"),
            "quote": StringField(value_string=quote, source="D(1,0,0,1,0,1,1,0,1)"),
        })]),
    }, ContentType.TEXT, "All project drives require vendor X.")
    assert bool(record.scopes) is grounded


def test_old_reduced_schema_is_rejected_instead_of_silently_losing_fields():
    with pytest.raises(ValueError, match="redeploy"):
        extract({"voltage": StringField(value_string="460 V")})
    with pytest.raises(ValueError, match="missing"):
        RoutedFieldExtractor(ContentType.TEXT).extract(context(ContentType.TEXT))


def test_field_source_outside_segment_is_not_accepted_as_evidence():
    record = extract({"tag": StringField(value_string="drive-X", source="D(20,0,0,1,0,1,1,0,1)")})
    assert not record.evidence