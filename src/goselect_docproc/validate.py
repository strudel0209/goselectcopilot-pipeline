"""Shape and evidence gates, never a claim of extraction accuracy."""

from jsonschema import Draft202012Validator

from .assemble import has_value
from .contracts import ExtractionPayload
from .field_schema import flat_fields, get_field, row_schema


def validate_payload(payload: ExtractionPayload, require_grounding: bool = True,
                     field_review_threshold: float | None = None) -> list[str]:
    errors = []
    validator = Draft202012Validator(row_schema())
    for index, record in enumerate(payload.records, 1):
        issues = list(record.issues)
        shape_errors = list(validator.iter_errors(record.row))
        issues.extend(f"{'.'.join(map(str, error.path))}: {error.message}" for error in shape_errors)
        if not shape_errors:
            for path in flat_fields():
                if not has_value(get_field(record.row, path)):
                    continue
                evidence = record.evidence.get(path, [])
                if require_grounding and not evidence:
                    issues.append(f"{path}: no native field evidence; verify source")
                if field_review_threshold is not None and any(
                    item.confidence is not None and item.confidence < field_review_threshold for item in evidence
                ):
                    issues.append(f"{path}: field confidence below threshold")
        record.issues = list(dict.fromkeys(issues))
        errors.extend(f"Row {index}: {issue}" for issue in record.issues)
    return errors