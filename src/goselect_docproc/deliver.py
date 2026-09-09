"""Customer JSON and readable review from the same already-assembled records."""

from copy import deepcopy

from jsonschema import validate

from .assemble import has_value, reading_paths
from .contracts import JobResult
from .field_schema import get_field, load_contract


def contract_payload(result: JobResult) -> dict:
    payload = {"vfd_motor_pairs": [deepcopy(record.row) for record in result.systems]}
    validate(payload, load_contract()["json_schema"])
    return payload


def display_value(value) -> str:
    if isinstance(value, dict):
        reading = " ".join(str(value[key]) for key in ("value", "unit") if value.get(key) is not None)
        return "; ".join(dict.fromkeys(part for part in [reading, value.get("details"), value.get("reference")] if part))
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def review_sheet(result: JobResult, *, document: str = "") -> str:
    lines = [f"# Extraction review{f' - {document}' if document else ''}", "",
             f"Job: **{result.status.value}**. {result.segments_done} completed, {result.segments_review} require review, "
             f"{result.segments_failed} failed out of {result.segments_expected} segments.", "",
             f"{len(result.systems)} assembled systems; {len(result.unassigned)} unassigned observations. Counts are not independently verified.",
             "Schema validity and worker completion do not establish accuracy. Unknown fields remain null in JSON; blank fields are omitted below.", ""]
    if result.review_required:
        lines += ["## Review required", "", *[f"- {cell(issue)}" for issue in result.review_required], ""]
    for label, records in (("System", result.systems), ("Unassigned", result.unassigned)):
        for index, record in enumerate(records, 1):
            lines += [f"## {label} {index}: {cell(record.row.get('tag') or 'no equipment identifier')}", "",
                      f"Motor: {cell(record.motor_tag or 'not extracted')}. Sources: {', '.join(record.sources)}.", "",
                      "| Field | Reading | Source |", "|---|---|---|"]
            for path in reading_paths():
                value = get_field(record.row, path)
                if path == "tag" or not has_value(value):
                    continue
                sources = [f"{item.file_id} p{item.page}: {item.verbatim or ''}"
                           for key, items in record.evidence.items() if key == path or key.startswith(path + ".")
                           for item in items]
                lines.append(f"| {cell(path)} | {cell(display_value(value))} | {cell('; '.join(dict.fromkeys(sources)) or 'verify source')} |")
            lines += ["", *[f"- {cell(issue)}" for issue in record.issues]]
            for path, evidence in record.scopes.items():
                lines.append(f"- Shared {path}: {cell(evidence.verbatim)} ({evidence.file_id} p{evidence.page})")
            for evidence in record.evidence.get("drawing", []):
                lines.append(f"- Drawing source: {evidence.file_id} p{evidence.page}: {cell(evidence.verbatim or '')}")
            lines.append("")
    if result.conflicts:
        lines += ["## Conflicts", "", "Precedence selects a provisional reading; conflicting readings remain unresolved.", ""]
        lines.extend(f"- {cell(conflict.field)}: " + " vs ".join(
            f"{cell(value)} ({origin.value})" for value, origin in zip(conflict.values, conflict.origins)) for conflict in result.conflicts)
    if result.notes:
        lines += ["", "## Extraction notes", "", *[f"- {cell(note)}" for note in result.notes]]
    return "\n".join(lines)