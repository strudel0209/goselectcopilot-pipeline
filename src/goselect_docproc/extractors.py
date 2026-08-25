"""Extraction branches - one per content type, behind a single protocol.

The model is a **replaceable dependency**, not a hard-coded call. That is what
makes benchmarking GPT against Claude per document family possible without
touching document logic.

Schema discipline, applied identically to every provider:

* the model returns a **flat, shallow, all-required** object with explicit nulls;
* nested UI shapes are expanded afterwards in deterministic Python.

This removes the whole class of "Claude omits nested or null fields" bugs, and
it is provider-independent, so the same schema works with OpenAI
``response_format``, Claude ``output_config.format`` and Claude forced tool use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .contracts import (
    ContentType,
    Evidence,
    ExtractionPayload,
    MotorSpec,
    Pair,
    Quantity,
    Span,
    VfdSpec,
    WorkItem,
)
from .reconcile import TagLexicon
from .deliver import plain
from .sections import SectionIndex
from .spans import text_for
from .tiling import Tile, VisionLimits, tile_image

log = logging.getLogger(__name__)

# Azure OpenAI rejects a request carrying more than this many images. A four-sheet
# drawing segment at 100 dpi produces 51 tiles, so batching is not optional.
MAX_IMAGES_PER_REQUEST = 50


@dataclass
class SegmentContext:
    """Everything a worker needs, resolved from the pointer in the WorkItem."""

    content: str
    item: WorkItem
    section_index: SectionIndex | None = None
    figures: dict[str, bytes] = field(default_factory=dict)
    lexicon: TagLexicon | None = None
    fields: Any = None
    """Contract fields the producer already extracted for this segment, if any."""

    @property
    def spans(self) -> list[tuple[int, int]]:
        return [s.as_tuple() for s in self.item.spans]

    @property
    def text(self) -> str:
        return text_for(self.content, self.spans)

    def evidence(self, *, page: int | None = None, source: str = "text", verbatim: str | None = None) -> Evidence:
        offset = min((o for o, _ in self.spans), default=0)
        return Evidence(
            file_id=self.item.file_id,
            page=page or self.item.first_page,
            spans=list(self.item.spans),
            section_path=(
                self.section_index.path_for(offset) if self.section_index else self.item.section_root
            ),
            verbatim=verbatim,
            source=source,  # type: ignore[arg-type]
        )


@runtime_checkable
class ModelClient(Protocol):
    """Provider seam. Implement once per Foundry deployment."""

    name: str

    def complete_json(self, *, prompt: str, schema: dict[str, Any], images: list[bytes] | None = None) -> dict[str, Any]: ...


@runtime_checkable
class Extractor(Protocol):
    content_type: ContentType

    def extract(self, context: SegmentContext) -> ExtractionPayload: ...


# ---------------------------------------------------------------------------
# flat schemas - every property required, absence expressed as null
# ---------------------------------------------------------------------------


def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


def _item_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _result_schema(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items", "notes"],
        "properties": {
            "items": {"type": "array", "items": item},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
    }


_SPEC_PROPERTIES: dict[str, Any] = {
    "kind": {"type": "string", "enum": ["MOTOR", "VFD", "APPLICATION"]},
    "tag": _nullable("string"),
    "power_raw": _nullable("string"),
    "voltage_raw": _nullable("string"),
    "current_raw": _nullable("string"),
    "frequency_raw": _nullable("string"),
    "poles": _nullable("integer"),
    "frame_size": _nullable("string"),
    "enclosure": _nullable("string"),
    "source_text": {"type": "string"},
}

# TEXT has no ``paired_with``. Specification prose states requirements that apply
# to every drive on the project; it does not pair a drive to a motor. Removing
# the field makes that structural instead of a request the model may ignore.
TEXT_SCHEMA = _result_schema(_item_schema(dict(_SPEC_PROPERTIES)))

# SCHEDULE rows carry a drive and its motor on one line, so pairing is in scope.
SCHEDULE_SCHEMA = _result_schema(
    _item_schema({**_SPEC_PROPERTIES, "paired_with": _nullable("string")})
)

# DRAWING is tags and topology only. Ratings read off a diagram are unreliable,
# and a field the model cannot fill honestly is a field it will fill dishonestly.
DRAWING_SCHEMA = _result_schema(
    _item_schema(
        {
            "kind": {"type": "string", "enum": ["MOTOR", "VFD", "OTHER"]},
            "tag": _nullable("string"),
            "paired_with": _nullable("string"),
            "source_text": {"type": "string"},
        }
    )
)

SCHEMAS: dict[ContentType, dict[str, Any]] = {
    ContentType.TEXT: TEXT_SCHEMA,
    ContentType.SCHEDULE: SCHEDULE_SCHEMA,
    ContentType.DRAWING: DRAWING_SCHEMA,
}

# Retained for callers that predate the split.
FLAT_ITEM_SCHEMA = SCHEDULE_SCHEMA["properties"]["items"]["items"]
FLAT_RESULT_SCHEMA = SCHEDULE_SCHEMA


def parse_quantity(raw: str | None) -> Quantity:
    """Deterministic. Never delegate unit parsing or arithmetic to a model."""
    if not raw:
        return Quantity()
    import re

    match = re.search(r"(-?\d+(?:[.,]\d+)?)\s*([A-Za-zΩ°/%]+)?", raw)
    if not match:
        return Quantity(raw=raw)
    value = float(match.group(1).replace(",", "."))
    return Quantity(value=value, unit=(match.group(2) or None), raw=raw)


def expand(items: list[dict[str, Any]], context: SegmentContext, origin: ContentType) -> ExtractionPayload:
    """Flat model output -> domain payload. Pure Python, fully testable."""
    payload = ExtractionPayload()
    lexicon = context.lexicon

    for index, item in enumerate(items):
        raw_tag = item.get("tag")
        tag = raw_tag
        if raw_tag and lexicon:
            repair = lexicon.snap(raw_tag)
            if repair.method == "ambiguous":
                payload.notes.append(
                    f"tag {raw_tag!r} ambiguous between {list(repair.ambiguous_with)}"
                )
            tag = repair.value

        evidence = context.evidence(source="table" if origin is ContentType.SCHEDULE else "text",
                                    verbatim=item.get("source_text"))
        kind = item.get("kind")

        if kind == "MOTOR":
            payload.motors.append(
                MotorSpec(
                    tag=tag,
                    power=parse_quantity(item.get("power_raw")),
                    voltage=parse_quantity(item.get("voltage_raw")),
                    frequency=parse_quantity(item.get("frequency_raw")),
                    poles=item.get("poles"),
                    frame_size=item.get("frame_size"),
                    ingress_protection=item.get("enclosure"),
                    evidence=[evidence],
                )
            )
        elif kind == "VFD":
            payload.vfds.append(
                VfdSpec(
                    tag=tag,
                    power=parse_quantity(item.get("power_raw")),
                    voltage=parse_quantity(item.get("voltage_raw")),
                    current=parse_quantity(item.get("current_raw")),
                    enclosure=item.get("enclosure"),
                    evidence=[evidence],
                )
            )

        partner = item.get("paired_with")
        if tag and partner:
            if lexicon:
                partner = lexicon.snap(partner).value
            vfd_tag, motor_tag = (tag, partner) if kind == "VFD" else (partner, tag)
            payload.pairs.append(
                Pair(
                    pair_id=f"{context.item.segment_id}-p{index}",
                    vfd_tag=vfd_tag,
                    motor_tag=motor_tag,
                    origin=origin,
                    confidence=0.6,
                    evidence=[evidence],
                )
            )

    return payload


# ---------------------------------------------------------------------------
# branches
# ---------------------------------------------------------------------------


PROMPTS = {
    ContentType.TEXT: (
        "You are reading a section of an engineering specification.\n"
        "Extract every motor, variable frequency drive and application requirement "
        "that is stated in this text.\n"
        "Rules: copy values verbatim into the *_raw fields, do not convert units, "
        "do not infer a value that is not written, and set null where the text is silent.\n"
        "Do not infer VFD-motor pairings from prose unless the text states the "
        "relationship explicitly.\n"
    ),
    ContentType.SCHEDULE: (
        "You are reading an equipment schedule rendered as a markdown table.\n"
        "Emit one item per row. Use the column headers to decide whether a row "
        "describes a motor or a drive.\n"
        "Rules: copy cell values verbatim into the *_raw fields, do not compute or "
        "convert anything, and set null for empty cells.\n"
        "Where a row names both a drive and a motor, set paired_with.\n"
    ),
    ContentType.DRAWING: (
        "You are reading tiles cropped from a single engineering drawing sheet at "
        "native resolution. Tiles overlap, so the same tag may appear more than once "
        "- report it each time you see it.\n"
        "Extract equipment tags and, where a line visibly connects a drive to a motor, "
        "the pairing.\n"
        "Rules: report only text you can actually read. If a character is unclear, "
        "reproduce exactly what is printed rather than guessing a plausible tag. "
        "Never expand or normalise a tag.\n"
    ),
}


@dataclass
class ModelExtractor:
    """Shared implementation. Branch differences are prompt, schema and input."""

    content_type: ContentType
    model: ModelClient
    schema: dict[str, Any] | None = None
    vision_limits: VisionLimits = field(default_factory=VisionLimits.high_resolution)
    max_tiles: int = 40

    def __post_init__(self) -> None:
        if self.schema is None:
            self.schema = SCHEMAS[self.content_type]

    def build_images(self, context: SegmentContext) -> tuple[list[bytes], list[str]]:
        if self.content_type is not ContentType.DRAWING:
            return [], []

        images: list[bytes] = []
        notes: list[str] = []
        for figure_id, blob in context.figures.items():
            tiles, whole, per_tile = tile_image(blob, self.vision_limits, max_tiles=self.max_tiles)
            if not whole.readable:
                notes.append(
                    f"figure {figure_id}: whole-sheet {whole.summary()}; "
                    f"tiled into {len(tiles)} at {per_tile.summary()}"
                )
            images.extend(t.png for t in tiles)
        return images, notes

    def extract(self, context: SegmentContext) -> ExtractionPayload:
        images, notes = self.build_images(context)
        prompt = PROMPTS[self.content_type]
        if context.item.section_root:
            prompt += f"\nThis content sits under section: {context.item.section_root}\n"
        prompt = f"{prompt}\n---\n{context.text}"

        batches: list[list[bytes] | None] = [None]
        if images:
            batches = [
                images[i : i + MAX_IMAGES_PER_REQUEST]
                for i in range(0, len(images), MAX_IMAGES_PER_REQUEST)
            ]
            if len(batches) > 1:
                notes.append(
                    f"{len(images)} tiles sent in {len(batches)} requests; the service "
                    f"caps one request at {MAX_IMAGES_PER_REQUEST} images"
                )

        items: list[dict[str, Any]] = []
        for batch in batches:
            response = self.model.complete_json(prompt=prompt, schema=self.schema, images=batch)
            items.extend(response.get("items", []))
            notes.extend(response.get("notes", []))

        payload = expand(items, context, self.content_type)
        payload.notes.extend(notes)
        return payload


class NullModel:
    """Deterministic stand-in so the pipeline is testable with no Azure spend."""

    name = "null"

    def __init__(self, canned: dict[str, Any] | None = None) -> None:
        self.canned = canned or {"items": [], "notes": []}
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, *, prompt: str, schema: dict[str, Any], images: list[bytes] | None = None) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "schema": schema, "images": len(images or [])})
        return self.canned


# ---------------------------------------------------------------------------
# contract fields -> domain payload
# ---------------------------------------------------------------------------

# Duck-typed on purpose: a Content Understanding ContentField exposes value,
# value_object, value_array, source, confidence and spans. Keeping this module
# free of the SDK import means the same code maps any producer that can supply
# the agreed contract shape.


def _obj(node: Any) -> dict[str, Any]:
    return getattr(node, "value_object", None) or {}


def _text(node: Any) -> str | None:
    if node is None:
        return None
    value = getattr(node, "value", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_source(source: str | None) -> tuple[int | None, list[float] | None]:
    """``D(page,x1,y1,...,x4,y4)`` -> page and polygon. Multi-region sources are
    separated by ``;``; the first region is the one a reviewer is sent to."""
    if not source:
        return None, None
    first = source.split(";")[0].strip()
    if not first.startswith("D(") or not first.endswith(")"):
        return None, None
    parts = [p.strip() for p in first[2:-1].split(",") if p.strip()]
    if not parts:
        return None, None
    try:
        page = int(float(parts[0]))
        coordinates = [float(p) for p in parts[1:]]
    except ValueError:
        return None, None
    return page, coordinates if len(coordinates) >= 8 else None


def _evidence(node: Any, context: SegmentContext, source: str) -> Evidence:
    page, polygon = parse_source(getattr(node, "source", None))
    spans = [
        Span(offset=int(s.offset or 0), length=int(s.length or 0))
        for s in (getattr(node, "spans", None) or [])
    ]
    return Evidence(
        file_id=context.item.file_id,
        page=page or context.item.first_page,
        spans=spans or list(context.item.spans),
        polygon=polygon,
        section_path=context.item.section_root,
        verbatim=_text(node),
        source=source,  # type: ignore[arg-type]
        confidence=getattr(node, "confidence", None),
    )


def _quantity(node: Any) -> Quantity:
    """The contract wraps every measurement as ``{value, unit, details}``."""
    body = _obj(node)
    raw = _text(body.get("value"))
    unit = _text(body.get("unit"))
    if raw is None:
        return Quantity()
    parsed = parse_quantity(f"{raw} {unit}" if unit else raw)
    return Quantity(value=parsed.value, unit=unit or parsed.unit, raw=raw)


def _valued(node: Any) -> str | None:
    """``{value, reference, details}`` groups such as enclosure_rating."""
    return _text(_obj(node).get("value"))


def expand_contract(
    fields: Any, context: SegmentContext, origin: ContentType
) -> ExtractionPayload:
    """Contract-shaped service output -> domain payload.

    The service returns the agreed schema; this only reshapes it and never
    reinterprets a value. Units and arithmetic stay in ``parse_quantity``.
    """
    payload = ExtractionPayload()
    pairs = fields.get("vfd_motor_pairs") if hasattr(fields, "get") else None
    for index, entry in enumerate(getattr(pairs, "value_array", None) or []):
        body = _obj(entry)
        payload.contract_rows.append(plain(entry))
        tag_node = body.get("tag")
        tag = _text(tag_node)
        if tag and context.lexicon:
            repair = context.lexicon.snap(tag)
            if repair.method == "ambiguous":
                payload.notes.append(
                    f"tag {tag!r} ambiguous between {list(repair.ambiguous_with)}"
                )
            tag = repair.value

        source = "table" if origin is ContentType.SCHEDULE else "text"
        evidence = _evidence(tag_node or entry, context, source)

        vfd = _obj(body.get("vfd"))
        motor = _obj(body.get("motor"))
        vfd_power = _obj(vfd.get("electrical"))
        motor_power = _obj(motor.get("electrical"))

        if vfd:
            payload.vfds.append(
                VfdSpec(
                    tag=tag,
                    power=_quantity(vfd_power.get("power_rating")),
                    voltage=_quantity(vfd_power.get("input_voltage")),
                    current=_quantity(vfd_power.get("output_current")),
                    enclosure=_valued(vfd.get("enclosure_rating")),
                    filter=_valued(vfd.get("harmonic_mitigation")),
                    evidence=[evidence],
                )
            )
        if motor:
            payload.motors.append(
                MotorSpec(
                    tag=tag,
                    power=_quantity(motor_power.get("power_rating")),
                    voltage=_quantity(motor_power.get("voltage")),
                    frequency=_quantity(motor_power.get("frequency")),
                    speed=_quantity(motor_power.get("speed")),
                    frame_size=_text(motor.get("frame_size")),
                    evidence=[evidence],
                )
            )
        # The contract keys a pair on one tag and gives the motor no identifier of
        # its own, so the pairing is one-sided. Only a grid asserts a relationship;
        # prose states requirements.
        if tag and vfd and motor and origin is ContentType.SCHEDULE:
            payload.pairs.append(
                Pair(
                    pair_id=f"{context.item.segment_id}-p{index}",
                    vfd_tag=tag,
                    motor_tag=None,
                    origin=origin,
                    confidence=float(getattr(tag_node, "confidence", None) or 0.0),
                    evidence=[evidence],
                )
            )


    if payload.pairs:
        payload.notes.append(
            f"{len(payload.pairs)} pairs are one-sided: the agreed contract has no motor tag "
            "field, so a motor is identified only by the pair it belongs to"
        )
    return payload


@dataclass
class RoutedFieldExtractor:
    """Uses fields the producer already extracted, or falls back to a model.

    Content Understanding can classify and extract in one call, so a routed
    segment arrives already extracted. A producer that cannot do that leaves
    ``context.fields`` empty and the fallback runs, which is what keeps the two
    engines comparable on the same corpus.
    """

    content_type: ContentType
    fallback: Extractor | None = None

    @property
    def model(self) -> Any:
        return getattr(self.fallback, "model", None)

    def extract(self, context: SegmentContext) -> ExtractionPayload:
        if context.fields:
            return expand_contract(context.fields, context, self.content_type)
        if self.fallback is not None:
            return self.fallback.extract(context)
        return ExtractionPayload(
            notes=[f"{self.content_type.value} {context.item.segment_id}: no fields returned"]
        )


@dataclass
class DropExtractor:
    """Out of scope by customer policy: segmented and coverage-proved, then not
    sent to a model. Dropping by *omitting* an extractor would work too, but it
    leaves no trace in the result, and silent exclusion is indistinguishable
    from a routing bug."""

    content_type: ContentType
    reason: str

    def extract(self, context: SegmentContext) -> ExtractionPayload:
        return ExtractionPayload(
            notes=[f"{self.content_type.value} {context.item.segment_id} not extracted: {self.reason}"]
        )


# A scaled layout shows where equipment sits, never which drive feeds which
# motor, so it cannot contribute a pairing - and its dimension strings read as
# plausible tags. The customer excluded plan sheets from scope.
DROP_REASONS: dict[ContentType, str] = {
    ContentType.PLAN: "plan sheets are excluded from scope; scaled layout carries no drive-motor topology",
}


def default_extractors(model: ModelClient, limits: VisionLimits | None = None) -> dict[ContentType, Extractor]:
    limits = limits or VisionLimits.high_resolution()
    extractors: dict[ContentType, Extractor] = {
        ct: ModelExtractor(content_type=ct, model=model, vision_limits=limits)
        for ct in (ContentType.TEXT, ContentType.SCHEDULE, ContentType.DRAWING)
    }
    # Prose and grids are routed to the service's own field extraction when the
    # producer supports it; the model call is the fallback, not the default.
    for ct in (ContentType.TEXT, ContentType.SCHEDULE):
        extractors[ct] = RoutedFieldExtractor(content_type=ct, fallback=extractors[ct])
    extractors.update(
        {ct: DropExtractor(content_type=ct, reason=reason) for ct, reason in DROP_REASONS.items()}
    )
    return extractors
