"""The agreed extraction contract, expressed as a Content Understanding field schema.

The customer's JSON Schema is the source of truth and is **converted**, not
restated, so agreeing a v1.1 is a file swap rather than a code change.

Two rules do the work:

* **Unions collapse to string.** ``ContentFieldType`` is a single type, while the
  contract declares ``value`` as ``number | string | null`` precisely so a reading
  like ``480/277`` survives. String keeps it verbatim; units and arithmetic stay
  in Python, which is the rule everywhere else in this pipeline.
* **Leaves are ``extract``, never ``generate``.** A generated value is not
  traceable to a place on a page, and a value with no evidence is rejected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from azure.ai.contentunderstanding.models import (
    ContentFieldDefinition,
    ContentFieldSchema,
    GenerationMethod,
)

# Anchored to the repository, not the working directory: a container job starts
# wherever the runtime puts it. GOSELECT_CONTRACT is the production override.
DEFAULT_CONTRACT = Path(__file__).resolve().parents[2] / "sample_docs" / "vfd_motor_schema_v1_0.json"

# ContentFieldType, minus the containers handled structurally below.
SCALARS = {"string", "number", "integer", "boolean", "date", "time"}


def contract_path() -> Path:
    return Path(os.getenv("GOSELECT_CONTRACT") or DEFAULT_CONTRACT)


def load_contract(path: Path | str | None = None) -> dict[str, Any]:
    return json.loads(Path(path or contract_path()).read_text(encoding="utf-8"))


def _semantic_type(declared: Any) -> str:
    """JSON Schema type -> ContentFieldType. ``null`` is dropped: absence is
    expressed by the field being missing, not by a null branch."""
    if isinstance(declared, list):
        real = [t for t in declared if t != "null"]
        if not real:
            return "string"
        if len(real) > 1:
            return "string"
        declared = real[0]
    return declared if declared in SCALARS or declared in ("array", "object") else "string"


def _convert(node: dict[str, Any]) -> ContentFieldDefinition:
    kind = _semantic_type(node.get("type"))
    description = node.get("description") or None

    if kind == "object":
        return ContentFieldDefinition(
            type="object",
            description=description,
            properties={
                name: _convert(child) for name, child in (node.get("properties") or {}).items()
            },
        )

    if kind == "array":
        return ContentFieldDefinition(
            type="array",
            description=description,
            item_definition=_convert(node.get("items") or {"type": "string"}),
        )

    definition = ContentFieldDefinition(
        type=kind, description=description, method=GenerationMethod.EXTRACT
    )
    if node.get("enum"):
        definition.enum = [str(value) for value in node["enum"] if value is not None]
    return definition


def to_field_schema(contract: dict[str, Any]) -> ContentFieldSchema:
    json_schema = contract.get("json_schema") or contract
    return ContentFieldSchema(
        name=contract.get("schema_name") or "goselect",
        description=contract.get("description"),
        fields={
            name: _convert(node) for name, node in (json_schema.get("properties") or {}).items()
        },
    )


def load_field_schema(path: Path | str | None = None) -> ContentFieldSchema:
    return to_field_schema(load_contract(path))


def count_fields(schema: ContentFieldSchema) -> int:
    """Named fields, the unit the 1,000-field service limit is expressed in."""

    def walk(definition: ContentFieldDefinition) -> int:
        total = 1
        for child in (definition.properties or {}).values():
            total += walk(child)
        if definition.item_definition is not None:
            total += walk(definition.item_definition)
        return total

    return sum(walk(field) for field in (schema.fields or {}).values())


# One analyzer per object type. The whole contract in one schema does not work:
# 112 fields x 42 rows truncates mid-document and never reaches the motor or
# electrical blocks. Each schema below asks only for what that object can carry,
# and the contract is assembled from them in Python.

SCHEDULE_ROW = {
    "type": "object",
    "properties": {
        "tag": {"type": "string", "description": "Equipment tag or ID for this drive, from the ID or TAG column"},
        "location": {"type": "string", "description": "Room, area or location column"},
        "serves": {"type": "string", "description": "What this drive serves: the system, area or equipment it feeds"},
        "manufacturer": {"type": "string", "description": "Basis of design manufacturer"},
        "model_number": {"type": "string", "description": "Basis of design model"},
        "enclosure_rating": {"type": "string", "description": "Enclosure rating column, e.g. NEMA 1"},
        "harmonic_mitigation": {"type": "string", "description": "Harmonic mitigation column"},
        "disconnect": {"type": "string", "description": "Disconnect column"},
        "bypass": {"type": "string", "description": "Bypass column"},
        "motor_power": {"type": "string", "description": "Motor power cell value, e.g. 45"},
        "motor_power_unit": {"type": "string", "description": "Unit for motor power, from the column header, e.g. HP or kW"},
        "motor_quantity": {"type": "string", "description": "Number of motors or drives on this row"},
        "motor_current": {"type": "string", "description": "Motor full load amps (FLA) with unit"},
        "motor_speed": {"type": "string", "description": "Motor speed with unit, e.g. 1750 RPM"},
        "voltage": {"type": "string", "description": "System voltage cell value, e.g. 460"},
        "voltage_unit": {"type": "string", "description": "Unit for the voltage column, from the header, e.g. V"},
        "phases": {"type": "string", "description": "Number of supply phases"},
        "frequency": {"type": "string", "description": "Supply frequency with unit, e.g. 60 Hz"},
        "sccr_rating": {"type": "string", "description": "Short circuit current rating"},
        "notes": {"type": "string", "description": "Note references for this row"},
    },
}

SCHEDULE_CONTRACT = {
    "schema_name": "goselectSchedule",
    "description": "One row per drive in a tabular equipment schedule",
    "json_schema": {
        "properties": {
            "rows": {
                "type": "array",
                "description": "One entry per data row of the drive or equipment schedule table",
                "items": SCHEDULE_ROW,
            }
        }
    },
}

TEXT_CONTRACT = {
    "schema_name": "goselectText",
    "description": "Requirements stated in specification prose",
    "json_schema": {
        "properties": {
            "requirements": {
                "type": "object",
                # Prose states what any drive must satisfy; it names no individual
                # equipment, so there is deliberately no tag and no pairing field.
                "description": "Drive and motor requirements that apply to the whole specification",
                "properties": {
                    "manufacturer": {"type": "string", "description": "Named acceptable manufacturer"},
                    "product_name": {"type": "string", "description": "Named acceptable product or series"},
                    "model_number": {"type": "string", "description": "Named acceptable model number"},
                    "input_voltage": {"type": "string", "description": "Required supply voltage with unit"},
                    "input_phases": {"type": "string", "description": "Required number of supply phases"},
                    "input_frequency": {"type": "string", "description": "Required supply frequency with unit"},
                    "output_frequency_min": {"type": "string", "description": "Minimum output frequency with unit"},
                    "output_frequency_max": {"type": "string", "description": "Maximum output frequency with unit"},
                    "enclosure_rating": {"type": "string", "description": "Required enclosure rating"},
                    "harmonic_mitigation": {"type": "string", "description": "Required harmonic mitigation or filter"},
                    "bypass": {"type": "string", "description": "Bypass requirement"},
                    "disconnect": {"type": "string", "description": "Disconnect requirement"},
                    "certifications": {
                        "type": "array",
                        "description": "Required certifications or listings",
                        "items": {"type": "string"},
                    },
                    "features": {
                        "type": "array",
                        "description": "Required features and options",
                        "items": {"type": "string"},
                    },
                    "ampacity_reference": {
                        "type": "string",
                        "description": "Where the specification says the ampacity ratings are stated, if it defers to another document",
                    },
                },
            }
        }
    },
}


def schedule_schema() -> ContentFieldSchema:
    return to_field_schema(SCHEDULE_CONTRACT)


def text_schema() -> ContentFieldSchema:
    return to_field_schema(TEXT_CONTRACT)


def empty_contract_row(contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """A contract-shaped row with every leaf null.

    Derived from the customer's own schema so the delivered shape cannot drift
    from the agreed one as fields are added.
    """
    schema = (contract or load_contract())["json_schema"]
    item = schema["properties"]["vfd_motor_pairs"]["items"]

    def build(node: dict[str, Any]) -> Any:
        declared = _semantic_type(node.get("type"))
        if declared == "object":
            return {k: build(v) for k, v in (node.get("properties") or {}).items()}
        if declared == "array":
            return []
        return None

    return build(item)
