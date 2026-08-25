"""The customer-facing end of the pipeline.

Everything upstream speaks the domain model in ``contracts``. GoSelect speaks
``sample_docs/vfd_motor_schema_v1_0.json``. This module is the one place that
translates outward, plus a review sheet a person can actually read.

Two artefacts, because two audiences:

``deliverable.json``
    Exactly the agreed schema, nothing added. This is what GoSelect consumes.

``review.md``
    The same values with page, confidence and section for each one, and a marker
    on anything below the review line. This is what a proposals engineer opens to
    decide whether the answer is right.

A field the pipeline could not fill is emitted as ``null`` rather than omitted or
guessed: a missing rating and an unread rating must not look the same.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .contracts import JobResult

REVIEW_LINE = 0.80
"""Fields below this are marked for a human. See ``PipelineConfig``."""


VALUE_KEYS = (
    "value_string",
    "value_number",
    "value_integer",
    "value_boolean",
    "value_date",
    "value_time",
)


def plain(node: Any) -> Any:
    """A service field node -> plain JSON in the agreed schema's own shape.

    Unwraps ``valueObject``/``valueArray``/``valueString`` and nothing else. A
    leaf the service left empty becomes ``None``, so an unread rating and an
    absent rating stay distinguishable downstream.
    """
    if node is None:
        return None
    obj = getattr(node, "value_object", None)
    if obj:
        return {k: plain(v) for k, v in obj.items()}
    arr = getattr(node, "value_array", None)
    if arr is not None:
        return [plain(v) for v in arr]
    for key in VALUE_KEYS:
        value = getattr(node, key, None)
        if value is not None:
            return value
    return None


def confidence_of(node: Any) -> float | None:
    return getattr(node, "confidence", None)


def _merge_rows(rows: list[dict]) -> list[dict]:
    """One row per tag, first non-null value wins.

    Two segments describing the same tag - a schedule row and a specification
    clause - must not produce two entries in the deliverable.
    """
    merged: "OrderedDict[str | None, dict]" = OrderedDict()
    for row in rows:
        tag = row.get("tag")
        if tag not in merged:
            merged[tag] = dict(row)
            continue
        target = merged[tag]
        for key, value in row.items():
            if target.get(key) is None:
                target[key] = value
            elif isinstance(value, dict) and isinstance(target.get(key), dict):
                _fill(target[key], value)
    return list(merged.values())


def _fill(target: dict, incoming: dict) -> None:
    for key, value in incoming.items():
        if target.get(key) is None:
            target[key] = value
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _fill(target[key], value)


def _populated(node: Any) -> int:
    """How many leaves actually carry a value."""
    if isinstance(node, dict):
        return sum(_populated(v) for v in node.values())
    if isinstance(node, list):
        return sum(_populated(v) for v in node)
    return 0 if node is None or node == "" else 1


def contract_payload(result: JobResult) -> dict[str, Any]:
    """The agreed GoSelect schema, built from the service's own rows."""
    return {"vfd_motor_pairs": _merge_rows(result.payload.contract_rows)}


def _flatten(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Leaf paths and values, for a flat human-readable table."""
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out += _flatten(value, f"{prefix} / {key}" if prefix else key)
    elif isinstance(node, list):
        if node:
            out.append((prefix, ", ".join(str(v) for v in node if v is not None)))
    else:
        out.append((prefix, node))
    return out


LABELS = {
    "vfd / electrical / power_rating": "Drive power",
    "vfd / electrical / input_voltage": "Drive input voltage",
    "vfd / electrical / output_current": "Drive output current",
    "vfd / product_name": "Drive product",
    "vfd / manufacturer": "Drive manufacturer",
    "vfd / model_number": "Drive model",
    "motor / electrical / power_rating": "Motor power",
    "motor / electrical / voltage": "Motor voltage",
    "motor / electrical / speed": "Motor speed",
}


def review_sheet(result: JobResult, *, document: str = "") -> str:
    """A sheet a proposals engineer can check without reading JSON."""
    rows = _merge_rows(result.payload.contract_rows)
    leaves = sum(len(_flatten(r)) for r in rows)
    filled = sum(_populated(r) for r in rows)

    lines = [f"# Extraction review{f' - {document}' if document else ''}", ""]
    lines += [
        f"- **{len(rows)}** equipment entries",
        f"- **{filled}** of {leaves} fields carry a value "
        f"({filled / leaves:.0%} of the agreed schema is populated)"
        if leaves
        else "- no fields extracted",
        f"- job **{result.status.value}**, "
        f"{result.segments_done}/{result.segments_expected} segments read",
    ]
    if result.conflicts:
        lines.append(f"- **{len(result.conflicts)}** conflicts, listed at the end")
    lines += ["", "Blank rows are fields the service did not find. They are shown", 
              "rather than hidden so gaps are visible.", "", "---", ""]

    for row in rows:
        tag = row.get("tag")
        lines.append(f"## {tag or '(tag not read)'}")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        for path, value in _flatten(row):
            if path == "tag":
                continue
            label = LABELS.get(path, path.replace(" / ", " "))
            shown = "-" if value is None or value == "" else str(value)
            lines.append(f"| {label} | {shown} |")
        lines.append("")

    if result.conflicts:
        lines += ["---", "", "## Conflicts", ""]
        for c in result.conflicts:
            origins = ", ".join(o.value for o in c.origins)
            lines.append(
                f"- **{c.field}**: {' vs '.join(c.values)} (from {origins})"
                f"{' - ' + c.resolution if c.resolution else ''}"
            )
        lines.append("")

    if result.payload.notes:
        lines += ["---", "", "## Notes from the extractor", ""]
        lines += [f"- {n}" for n in result.payload.notes]
        lines.append("")

    return "\n".join(lines)
