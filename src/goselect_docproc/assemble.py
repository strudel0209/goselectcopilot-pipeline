"""Assemble once; keep source observations in worker results, not duplicate models."""

from copy import deepcopy
from datetime import datetime, timezone

from .contracts import Conflict, ContentType, ExtractionRecord, JobResult, Manifest, SegmentResult, Status
from .field_schema import empty_contract_row, flat_fields, get_field, scope_paths, set_field
from .spans import text_for


SPEC_PRECEDENCE = (ContentType.SCHEDULE, ContentType.TEXT, ContentType.DRAWING, ContentType.OTHER)


def document_order(manifest: Manifest, results: list[SegmentResult]) -> list[SegmentResult]:
    latest = {result.segment_id: result for result in results}
    return sorted(latest.values(), key=lambda result: (
        manifest.file(result.file_id).ordinal, result.start_offset, result.segment_id,
    ))


def completion(manifest: Manifest, results: list[SegmentResult]) -> tuple[int, int, int]:
    results = document_order(manifest, results)
    return manifest.expected_units, sum(result.status is Status.DONE for result in results), sum(result.status is Status.FAILED for result in results)


def reassemble_markdown(manifest: Manifest, content_by_file: dict[str, str], separator: str = "\n\n") -> str:
    return separator.join(text_for(content_by_file[segment.file_id], [span.as_tuple() for region in segment.regions for span in region.spans])
                          for segment in sorted(manifest.segments, key=manifest.sort_key))


def has_value(value) -> bool:
    if isinstance(value, dict):
        return has_value(value.get("value")) or has_value(value.get("details"))
    return value not in (None, "", [], {})


def reading_paths() -> list[str]:
    return list(dict.fromkeys(path.rsplit(".", 1)[0] if path.endswith((".value", ".unit", ".details", ".reference")) else path
                              for path in flat_fields()))


def merge_records(records: list[ExtractionRecord], conflicts: list[Conflict]) -> ExtractionRecord:
    records = sorted(records, key=lambda record: SPEC_PRECEDENCE.index(record.origin))
    merged = records[0].model_copy(deep=True)
    origins = {path: merged.origin for path in reading_paths()}
    for incoming in records[1:]:
        for path in reading_paths():
            value, held = get_field(incoming.row, path), get_field(merged.row, path)
            if not has_value(value):
                continue
            if not has_value(held):
                set_field(merged.row, path, deepcopy(value))
                origins[path] = incoming.origin
            elif isinstance(value, list):
                set_field(merged.row, path, list(dict.fromkeys(held + value)))
            elif path == "notes":
                merged.row[path] = "\n".join(dict.fromkeys([held, value]))
            elif path != "tag":
                previous = (held.get("value"), held.get("unit")) if isinstance(held, dict) else held
                candidate = (value.get("value"), value.get("unit")) if isinstance(value, dict) else value
                if previous != candidate:
                    conflicts.append(Conflict(field=f"{merged.row['tag']}.{path}", values=[str(held), str(value)], origins=[origins[path], incoming.origin]))
        merged.motor_tag = merged.motor_tag or incoming.motor_tag
        merged.identified_system |= incoming.identified_system
        merged.sources = list(dict.fromkeys(merged.sources + incoming.sources))
        merged.issues = list(dict.fromkeys(merged.issues + incoming.issues))
        for path, evidence in incoming.evidence.items():
            held = merged.evidence.setdefault(path, [])
            held.extend(item for item in evidence if item not in held)
        merged.scopes.update(incoming.scopes)
    return merged


def merge(manifest: Manifest, results: list[SegmentResult], review_threshold: float = 0.25) -> JobResult:
    segments = {segment.segment_id: segment for segment in manifest.segments}
    for result in results:
        segment = segments.get(result.segment_id)
        if (result.job_id != manifest.job_id or not segment or result.file_id != segment.file_id
                or result.content_type is not segment.content_type):
            raise ValueError("Results must belong to this manifest")
    ordered = document_order(manifest, results)
    expected, done, failed = completion(manifest, ordered)
    records, notes, review, trusted = [], [], [], set()
    for result in ordered:
        segment = segments[result.segment_id]
        if result.status is not Status.DONE or segment.confidence < review_threshold:
            review.append(f"{result.segment_id}: {result.status.value}; " + "; ".join(result.errors))
        else:
            trusted.add(result.segment_id)
        if result.payload and result.status is not Status.FAILED:
            notes.extend(result.payload.notes)
            for original in result.payload.records:
                record = original.model_copy(deep=True)
                record.origin, record.sources = result.content_type, [result.segment_id]
                record.scopes = {path: evidence for path, evidence in record.scopes.items()
                                 if result.segment_id in trusted and evidence.file_id == result.file_id}
                records.append(record)
    groups, unassigned, shared = [], [], []
    for record in records:
        tag = (record.row.get("tag") or "").strip()
        if not tag or not record.identified_system or record.origin not in {ContentType.SCHEDULE, ContentType.DRAWING}:
            unassigned.append(record)
            continue
        matches = [group for group in groups if group[0].row["tag"].strip().casefold() == tag.casefold()
                   and not (record.motor_tag and any(member.motor_tag and member.motor_tag != record.motor_tag for member in group))
                   and not (record.origin is ContentType.SCHEDULE and any(set(member.sources) & set(record.sources) for member in group))]
        if len(matches) == 1:
            matches[0].append(record)
        else:
            groups.append([record])
    confirmed = [group for group in groups if any(record.sources[0] in trusted for record in group)]
    if confirmed:
        for record in unassigned:
            if record.origin is not ContentType.TEXT or record.row.get("tag"):
                continue
            projection = ExtractionRecord(row=empty_contract_row(), origin=record.origin, sources=record.sources)
            for path, evidence in list(record.scopes.items()):
                if path not in scope_paths() or not has_value(get_field(record.row, path)):
                    continue
                set_field(projection.row, path, deepcopy(get_field(record.row, path)))
                projection.scopes[path] = evidence
                for key in list(record.evidence):
                    if key == path or key.startswith(path + "."):
                        projection.evidence[key] = record.evidence.pop(key)
                set_field(record.row, path, get_field(empty_contract_row(), path))
                record.scopes.pop(path)
            if projection.scopes:
                shared.append(projection)
    conflicts = []
    systems = [merge_records(group + (shared if group in confirmed else []), conflicts) for group in groups]
    unassigned = [record for record in unassigned if any(has_value(get_field(record.row, path)) for path in reading_paths())]
    for system in systems:
        if not system.motor_tag:
            system.issues.append("Motor identifier not extracted")
        if any(evidence.source == "derived" for values in system.evidence.values() for evidence in values):
            system.issues.append("Verify drawing model readings and motor connection against source image")
        if sum(other.row["tag"] == system.row["tag"] for other in systems) > 1:
            system.issues.append("Repeated identifier or different motor endpoints; records kept separate")
        review.extend(f"{system.row['tag']}: {issue}" for issue in system.issues)
    if unassigned:
        review.append("Unassigned observations or requirements need applicability review")
    if not systems:
        review.append("No identified VFD systems extracted")
    review.extend(conflict.field for conflict in conflicts)
    review.extend(f"{file_id}: incomplete returned-markdown coverage" for file_id, coverage in manifest.coverage.items() if not coverage.ok)
    review.extend(f"{segment_id}: missing result" for segment_id in segments.keys() - {result.segment_id for result in ordered})
    status = Status.DONE if done == expected and not review else Status.REVIEW
    if not any(result.status is not Status.FAILED for result in ordered):
        status = Status.FAILED
    return JobResult(job_id=manifest.job_id, correlation_id=manifest.correlation_id, status=status,
                     segments_expected=expected, segments_done=done, segments_failed=failed,
                     segments_review=sum(result.status is Status.REVIEW for result in ordered),
                     systems=systems, unassigned=unassigned, notes=list(dict.fromkeys(notes)),
                     conflicts=conflicts, review_required=sorted(set(review)), coverage=manifest.coverage,
                     completed_at=datetime.now(timezone.utc))