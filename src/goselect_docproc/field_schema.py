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
