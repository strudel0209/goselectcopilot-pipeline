"""The agreed contract, converted to a Content Understanding field schema.

The customer's JSON Schema is the artefact they sign. These tests pin the
conversion rules that a signature cannot express.
"""

from __future__ import annotations

import pytest
from azure.ai.contentunderstanding.models import GenerationMethod

from goselect_docproc.field_schema import (
    contract_path,
    count_fields,
    load_contract,
    load_field_schema,
    to_field_schema,
)


@pytest.fixture(scope="module")
def schema():
    return load_field_schema()


def leaf(schema, *path):
    node = schema.fields[path[0]]
    for step in path[1:]:
        node = node.item_definition if step == "[]" else node.properties[step]
    return node


class TestContractConversion:
    def test_the_customer_contract_is_the_source(self):
        contract = load_contract()
        assert contract["schema_name"] == "vfd_motor_schema"
        assert contract_path().exists()

    def test_the_root_is_the_pair_list(self, schema):
        assert list(schema.fields) == ["vfd_motor_pairs"]
        assert schema.fields["vfd_motor_pairs"].type == "array"

    def test_nesting_survives_to_the_leaf(self, schema):
        node = leaf(schema, "vfd_motor_pairs", "[]", "vfd", "electrical", "input_voltage", "value")
        assert node.method == GenerationMethod.EXTRACT
        assert node.description

    def test_unions_collapse_to_string_so_480_277_survives(self, schema):
        """The contract declares number|string for exactly this reason."""
        node = leaf(schema, "vfd_motor_pairs", "[]", "vfd", "electrical", "input_voltage", "value")
        assert node.type == "string"

    def test_a_plain_number_stays_a_number(self, schema):
        node = leaf(schema, "vfd_motor_pairs", "[]", "vfd", "quantity")
        assert node.type == "number"

    def test_every_leaf_is_extracted_never_generated(self, schema):
        """A generated value has no place on a page, so it cannot be reviewed."""

        def check(node):
            if node.type == "object":
                for child in (node.properties or {}).values():
                    check(child)
            elif node.type == "array":
                check(node.item_definition)
            else:
                assert node.method == GenerationMethod.EXTRACT

        for field in schema.fields.values():
            check(field)

    def test_it_fits_the_service_limit(self, schema):
        assert count_fields(schema) <= 1000

    def test_nulls_are_dropped_rather_than_typed(self):
        converted = to_field_schema(
            {"json_schema": {"properties": {"tag": {"type": ["string", "null"]}}}}
        )
        assert converted.fields["tag"].type == "string"

    def test_enums_are_carried_across(self):
        converted = to_field_schema(
            {"json_schema": {"properties": {"kind": {"type": "string", "enum": ["A", "B", None]}}}}
        )
        assert converted.fields["kind"].enum == ["A", "B"]
