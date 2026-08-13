"""Validation gates.

Two independent checks, both cheap and both mandatory before a value reaches a
quotation:

1. **Schema** - Pydantic already guarantees shape; this adds domain rules that a
   type system cannot express.
2. **Grounding** - a value with no page/span evidence is rejected, not trusted.
   This is what makes the human review loop and the audit trail possible.
"""

from __future__ import annotations

from .contracts import ExtractionPayload, MotorSpec, Pair, Quantity, VfdSpec

# Sanity envelopes. Values outside these are almost always a unit or OCR error,
# not a real specification.
PLAUSIBLE = {
    "power_kw": (0.01, 100_000.0),
    "voltage_v": (24.0, 40_000.0),
    "current_a": (0.1, 20_000.0),
    "frequency_hz": (10.0, 400.0),
}

_UNIT_ALIASES = {
    "kw": "kw", "kilowatt": "kw", "hp": "hp",
    "v": "v", "volt": "v", "kv": "kv",
    "a": "a", "amp": "a", "amps": "a",
    "hz": "hz",
}


def _canonical_unit(unit: str | None) -> str | None:
    return _UNIT_ALIASES.get((unit or "").strip().lower()) if unit else None


def _check_range(label: str, quantity: Quantity, key: str, errors: list[str]) -> None:
    if quantity.value is None:
        return
    low, high = PLAUSIBLE[key]
    value = quantity.value
    unit = _canonical_unit(quantity.unit)
    if key == "power_kw" and unit == "hp":
        value *= 0.7457
    if key == "voltage_v" and unit == "kv":
        value *= 1000.0
    if not low <= value <= high:
        errors.append(f"{label}: {quantity.raw or value!r} outside plausible range {low}-{high}")


def validate_spec(spec: MotorSpec | VfdSpec, errors: list[str], require_grounding: bool) -> None:
    label = spec.tag or f"<untagged {type(spec).__name__}>"
    if require_grounding and not spec.evidence:
        errors.append(f"{label}: no evidence, rejected")

    _check_range(f"{label}.power", spec.power, "power_kw", errors)
    _check_range(f"{label}.voltage", spec.voltage, "voltage_v", errors)
    if isinstance(spec, VfdSpec):
        _check_range(f"{label}.current", spec.current, "current_a", errors)
    if isinstance(spec, MotorSpec):
        _check_range(f"{label}.frequency", spec.frequency, "frequency_hz", errors)
        if spec.poles is not None and (spec.poles < 2 or spec.poles % 2):
            errors.append(f"{label}.poles: {spec.poles} is not an even number >= 2")


def validate_pair(pair: Pair, errors: list[str], require_grounding: bool) -> None:
    if not pair.vfd_tag and not pair.motor_tag:
        errors.append(f"{pair.pair_id}: pair has neither tag")
    if pair.vfd_tag and pair.vfd_tag == pair.motor_tag:
        errors.append(f"{pair.pair_id}: drive and motor share tag {pair.vfd_tag}")
    if require_grounding and not pair.evidence:
        errors.append(f"{pair.pair_id}: no evidence, rejected")


def validate_payload(payload: ExtractionPayload, require_grounding: bool = True) -> list[str]:
    errors: list[str] = []
    for spec in [*payload.motors, *payload.vfds]:
        validate_spec(spec, errors, require_grounding)
    for pair in payload.pairs:
        validate_pair(pair, errors, require_grounding)

    known = {s.tag for s in [*payload.motors, *payload.vfds] if s.tag}
    for pair in payload.pairs:
        for tag in (pair.vfd_tag, pair.motor_tag):
            if tag and known and tag not in known:
                errors.append(f"{pair.pair_id}: references unknown tag {tag}")
    return errors
