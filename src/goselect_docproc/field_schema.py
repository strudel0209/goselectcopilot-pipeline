"""One reversible flat extraction schema, derived from the customer contract."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path

from azure.ai.contentunderstanding.models import ContentFieldDefinition, ContentFieldSchema


DEFAULT_CONTRACT = Path(__file__).resolve().parents[2] / "sample_docs" / "vfd_motor_schema_v1_0.json"


def contract_path() -> Path:
    return Path(os.getenv("GOSELECT_CONTRACT") or DEFAULT_CONTRACT)


def load_contract(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or contract_path()).read_text(encoding="utf-8"))


def row_schema(contract: dict | None = None) -> dict:
    return (contract or load_contract())["json_schema"]["properties"]["vfd_motor_pairs"]["items"]


def flat_fields(node: dict | None = None, prefix: str = "", context: str = "") -> dict[str, dict]:
    node = row_schema() if node is None else node
    description = "; ".join(part for part in (context, node.get("description")) if part)
    if node.get("type") == "object":
        return {path: definition for name, child in node["properties"].items()
                for path, definition in flat_fields(child, f"{prefix}.{name}" if prefix else name, description).items()}
    return {prefix: {**deepcopy(node), "description": f"{prefix}: {description}"}}


def empty_contract_row(contract: dict | None = None) -> dict:
    def empty(node):
        if node.get("type") == "object":
            return {name: empty(child) for name, child in node["properties"].items()}
        return [] if node.get("type") == "array" else None
    return empty(row_schema(contract))


def set_field(row: dict, path: str, value) -> None:
    parts = path.split(".")
    target = row
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def get_field(row: dict, path: str):
    for part in path.split("."):
        row = row[part]
    return row


def scope_paths() -> list[str]:
    return sorted({path.rsplit(".", 1)[0] if path.endswith(".value") else path
                   for path in flat_fields() if path.startswith("vfd.")
                   and not path.endswith((".unit", ".details", ".reference")) and path != "vfd.quantity"})


def extraction_schema() -> dict:
    fields = flat_fields()
    fields.update({
        "motor_tag": {"type": ["string", "null"], "description": "Motor identifier explicitly associated with this VFD; never invent an identifier."},
        "identified_system": {"type": "boolean", "description": "True when the source identifies a specific equipment system through equipment identifiers, stated associations or shown connections,regardless of controller type or whether the evidence appears in text, a table or a drawing. False when the observation contains only general requirements, an illustrative example or insufficient evidence to identify a specific system. Do not invent equipment or connections."},
        "all_vfds_scope": {
            "type": "array", "description": "Only explicit requirements applying to ALL project VFDs without exceptions. Never infer scope from a heading. No motor requirements. For a list, the quote must cover every item.",
            "items": {"type": "object", "properties": {
                "field": {"type": "string", "enum": scope_paths()},
                "quote": {"type": "string", "description": "Exact source clause establishing the requirement and its applicability to all project VFDs."},
            }, "required": ["field", "quote"], "additionalProperties": False},
        },
    })
    return {"type": "object", "properties": {"rows": {
        "type": "array",
        "description": "One entry per VFD-motor system or distinct unassigned requirement group. Copy stated values and printed units, including table headers. Do not infer ratings, turn minimum ampacity into FLA, or transfer upstream equipment ratings. Retain qualified readings in notes. Leave unknown fields null and lists empty. Never invent equipment or combine unrelated requirements.",
        "items": {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False},
    }}, "required": ["rows"], "additionalProperties": False}


def to_field_schema(schema: dict) -> ContentFieldSchema:
    def convert(node):
        declared = node.get("type", "string")
        kinds = [kind for kind in declared if kind != "null"] if isinstance(declared, list) else [declared]
        kind = kinds[0] if len(kinds) == 1 else "string"
        definition = ContentFieldDefinition(type=kind, description=node.get("description"))
        if kind == "object":
            definition.properties = {name: convert(child) for name, child in node["properties"].items()}
        elif kind == "array":
            definition.item_definition = convert(node["items"])
        else:
            definition.method = "extract"
            if "enum" in node:
                definition.enum = node["enum"]
        return definition
    body = schema.get("json_schema", schema)
    return ContentFieldSchema(fields={name: convert(node) for name, node in body["properties"].items()})


def native_field_paths() -> dict[str, str]:
    properties = extraction_schema()["properties"]["rows"]["items"]["properties"]
    paths = {path.replace(".", "__"): path for path in properties}
    if len(paths) != len(properties):
        raise ValueError("Customer paths collide after CU field-name encoding")
    return paths


def load_field_schema() -> ContentFieldSchema:
    schema = to_field_schema(extraction_schema())
    row = schema.fields["rows"].item_definition
    row.properties = {name: row.properties[path] for name, path in native_field_paths().items()}
    return schema