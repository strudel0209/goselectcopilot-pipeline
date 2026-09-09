import re

import pytest
from azure.ai.contentunderstanding.models import ContentFieldSchema
from jsonschema import validate

from goselect_docproc.field_schema import (
    empty_contract_row, extraction_schema, flat_fields, get_field, load_contract,
    load_field_schema, native_field_paths, scope_paths, set_field,
)


def test_every_customer_leaf_round_trips_without_a_mapping_table():
    row = empty_contract_row()
    for path, definition in flat_fields().items():
        kinds = definition["type"]
        value = ["stated"] if kinds == "array" else 7 if "number" in kinds else "stated"
        set_field(row, path, value)
        assert get_field(row, path) == value
    validate({"vfd_motor_pairs": [row]}, load_contract()["json_schema"])


def test_schema_is_native_and_covers_every_customer_field():
    restored = ContentFieldSchema(load_field_schema().as_dict())
    encoded = restored.fields["rows"].item_definition.properties
    assert set(encoded) == set(native_field_paths())
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in encoded)
    assert "motor__electrical__voltage__value" in encoded
    properties = {native_field_paths()[name]: definition for name, definition in encoded.items()}
    assert set(flat_fields()) <= set(properties)
    assert properties["motor.electrical.service_factor.value"].type == "number"
    assert properties["motor.electrical.voltage.value"].type == "string"
    assert properties["vfd.features"].type == "array"
    assert "Motor nameplate power" in properties["motor.electrical.power_rating.value"].description
    assert properties["motor.electrical.power_rating.value"].method == "extract"


def test_scope_is_metadata_and_cannot_authorize_motor_or_quantity_fields():
    assert all(path.startswith("vfd.") for path in scope_paths())
    assert "vfd.quantity" not in scope_paths()
    assert "vfd.electrical.input_voltage" in scope_paths()
    assert "all_vfds_scope" not in str(load_contract())
    assert extraction_schema()["additionalProperties"] is False


def test_new_contract_fields_are_not_lost_by_flattening():
    assert flat_fields({"type": "object", "properties": {
        "new_field": {"type": ["string", "null"], "description": "New customer field"},
    }})["new_field"]["description"] == "new_field: New customer field"


def test_native_name_collisions_fail_instead_of_losing_fields(monkeypatch):
    monkeypatch.setattr("goselect_docproc.field_schema.flat_fields", lambda: {
        "example.value": {"type": "string"}, "example__value": {"type": "string"},
    })
    with pytest.raises(ValueError, match="collide"):
        native_field_paths()