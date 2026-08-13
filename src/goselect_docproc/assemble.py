"""Reassembly and cross-segment merge.

Segments are span ranges over one immutable ``content`` string, never copies of
it. Workers may therefore finish in any order across any number of queues: the
global ordering key is ``(file_ordinal, span_offset)`` and reassembly is a sort.

Page number is **not** a valid ordering key - intra-page regions share one.

Merge precedence is stated, not implicit:

* numeric specifications  -> SCHEDULE > TEXT > DRAWING  (grids are authoritative)
* pairing / topology      -> DRAWING > SCHEDULE > TEXT  (the diagram shows wiring)

Disagreement is recorded as a ``Conflict`` and routed to review. It is never
silently resolved.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .contracts import (
    Conflict,
    ContentType,
    Coverage,
    Evidence,
    ExtractionPayload,
    JobResult,
    Manifest,
    MotorSpec,
    Pair,
    Quantity,
    SegmentResult,
    Status,
    VfdSpec,
)
from .spans import text_for

SPEC_PRECEDENCE = (ContentType.SCHEDULE, ContentType.TEXT, ContentType.DRAWING)
PAIR_PRECEDENCE = (ContentType.DRAWING, ContentType.SCHEDULE, ContentType.TEXT)


def document_order(manifest: Manifest, results: list[SegmentResult]) -> list[SegmentResult]:
    ordinals = {f.file_id: f.ordinal for f in manifest.files}
    return sorted(results, key=lambda r: (ordinals.get(r.file_id, r.file_ordinal), r.start_offset))


def reassemble_markdown(manifest: Manifest, content_by_file: dict[str, str], separator: str = "\n\n") -> str:
    """Rebuild readable document order for audit and human review.

    The canonical markdown is never mutated, so this is a projection, not a
    merge. Only build it for a consumer that actually reads prose.
    """
    chunks: list[str] = []
    for segment in sorted(manifest.segments, key=manifest.sort_key):
        content = content_by_file.get(segment.file_id)
        if not content:
            continue
        spans = [s.as_tuple() for r in segment.regions for s in r.spans]
        if spans:
            chunks.append(text_for(content, spans))
    return separator.join(chunks)


def completion(manifest: Manifest, results: list[SegmentResult]) -> tuple[int, int, int]:
    """``(expected, done, failed)`` counted from **distinct** segment ids.

    Counting distinct documents rather than incrementing a counter is what makes
    at-least-once delivery harmless.
    """
    seen: dict[str, SegmentResult] = {}
    for r in results:
        if r.status.terminal or r.segment_id not in seen:
            seen[r.segment_id] = r
    done = sum(1 for r in seen.values() if r.status is Status.DONE)
    failed = sum(1 for r in seen.values() if r.status is Status.FAILED)
    return manifest.expected_units, done, failed


def _rank(origin: ContentType, order: tuple[ContentType, ...]) -> int:
    return order.index(origin) if origin in order else len(order)


# Not a tag: the key under which untagged project-wide requirements consolidate.
UNTAGGED = "\x00project"


def _merge_quantity(current: Quantity, incoming: Quantity, take: bool) -> Quantity:
    if take and (incoming.value is not None or incoming.raw):
        return incoming
    return current


def _merge_specs(items: list[tuple[ContentType, MotorSpec | VfdSpec]], conflicts: list[Conflict]):
    """Merge same-tag specs, preferring the highest-precedence non-null value.

    Specs with no tag are **project-level requirements**, not noise: a Division 16
    clause such as "VFDs shall be ABB ACQ580, no equal" applies to every drive on
    the job and names none of them. They consolidate into a single untagged spec
    rather than being dropped, which is what previously discarded an entire
    specification section.
    """
    by_tag: dict[str, tuple[ContentType, MotorSpec | VfdSpec]] = {}
    for origin, spec in items:
        tag = (spec.tag or "").strip().upper()
        key = tag or UNTAGGED
        label = tag or "<project>"
        if key not in by_tag:
            by_tag[key] = (origin, spec.model_copy(deep=True))
            continue

        held_origin, held = by_tag[key]
        take = _rank(origin, SPEC_PRECEDENCE) < _rank(held_origin, SPEC_PRECEDENCE)

        for name, field in type(held).model_fields.items():
            if name in {"tag", "evidence"}:
                continue
            incoming_value = getattr(spec, name)
            held_value = getattr(held, name)
            if isinstance(held_value, Quantity):
                merged = _merge_quantity(held_value, incoming_value, take)
                if (
                    held_value.value is not None
                    and incoming_value.value is not None
                    and held_value.value != incoming_value.value
                ):
                    conflicts.append(
                        Conflict(
                            field=f"{label}.{name}",
                            values=[str(held_value.value), str(incoming_value.value)],
                            origins=[held_origin, origin],
                        )
                    )
                setattr(held, name, merged)
            elif isinstance(held_value, list):
                setattr(held, name, held_value + [v for v in incoming_value if v not in held_value])
            else:
                if held_value is None:
                    setattr(held, name, incoming_value)
                elif incoming_value is not None and incoming_value != held_value:
                    conflicts.append(
                        Conflict(
                            field=f"{label}.{name}",
                            values=[str(held_value), str(incoming_value)],
                            origins=[held_origin, origin],
                        )
                    )
                    if take:
                        setattr(held, name, incoming_value)

        held.evidence = _dedupe_evidence(held.evidence + spec.evidence)
        if take:
            by_tag[key] = (origin, held)

    return [spec for _, spec in by_tag.values()]


def _dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    seen: dict[tuple, Evidence] = {}
    for e in items:
        key = (e.file_id, e.page, tuple(s.as_tuple() for s in e.spans), e.source)
        seen.setdefault(key, e)
    return sorted(seen.values(), key=lambda e: (e.file_id, e.start))


def _merge_pairs(pairs: list[Pair]) -> list[Pair]:
    """Corroboration across segment types raises confidence; it never invents pairs."""
    grouped: dict[tuple, list[Pair]] = {}
    for p in pairs:
        key = (
            (p.vfd_tag or "").strip().upper() or None,
            (p.motor_tag or "").strip().upper() or None,
        )
        grouped.setdefault(key, []).append(p)

    merged: list[Pair] = []
    for key, group in grouped.items():
        best = min(group, key=lambda p: _rank(p.origin, PAIR_PRECEDENCE))
        origins = {p.origin for p in group}
        corroboration = min(0.15 * (len(origins) - 1), 0.3)
        merged.append(
            Pair(
                pair_id=best.pair_id,
                vfd_tag=key[0],
                motor_tag=key[1],
                origin=best.origin,
                confidence=round(min(1.0, max(p.confidence for p in group) + corroboration), 3),
                evidence=_dedupe_evidence([e for p in group for e in p.evidence]),
            )
        )
    return sorted(merged, key=lambda p: (p.vfd_tag or "", p.motor_tag or ""))


def merge(
    manifest: Manifest,
    results: list[SegmentResult],
    review_threshold: float = 0.25,
) -> JobResult:
    ordered = document_order(manifest, results)
    expected, done, failed = completion(manifest, results)
    conflicts: list[Conflict] = []

    motors, vfds, applications, pairs, notes = [], [], [], [], []
    unverified: list[str] = []
    for r in ordered:
        # REVIEW is included on purpose. Its payload is exactly what a human has to
        # check; discarding it hands the reviewer a blank page and the extraction
        # gets redone by hand. FAILED has nothing to contribute.
        if r.status is Status.FAILED or r.payload is None:
            continue
        if r.status is Status.REVIEW:
            unverified.append(r.segment_id)
        motors += [(r.content_type, m) for m in r.payload.motors]
        vfds += [(r.content_type, v) for v in r.payload.vfds]
        applications += r.payload.applications
        pairs += r.payload.pairs
        notes += r.payload.notes

    if unverified:
        notes.append(
            "Unverified content from segments needing review: " + ", ".join(unverified)
        )

    payload = ExtractionPayload(
        motors=_merge_specs(motors, conflicts),
        vfds=_merge_specs(vfds, conflicts),
        applications=applications,
        pairs=_merge_pairs(pairs),
        notes=notes,
    )

    review = [s.segment_id for s in manifest.segments if s.confidence < review_threshold]
    review += [r.segment_id for r in ordered if r.status in {Status.FAILED, Status.REVIEW}]
    review += [c.field for c in conflicts]

    status = Status.DONE if done == expected and not failed else Status.REVIEW
    if done == 0:
        status = Status.FAILED

    return JobResult(
        job_id=manifest.job_id,
        correlation_id=manifest.correlation_id,
        status=status,
        segments_expected=expected,
        segments_done=done,
        segments_failed=failed,
        payload=payload,
        conflicts=conflicts,
        review_required=sorted(set(review)),
        coverage=manifest.coverage,
        completed_at=datetime.now(timezone.utc),
    )
