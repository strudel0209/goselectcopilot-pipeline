"""Native CU field mapping and an isolated drawing-only vision fallback."""

from dataclasses import dataclass, field
from typing import Protocol

from azure.ai.contentunderstanding.models import ContentField
from jsonschema import validate

from .contracts import ContentType, Evidence, ExtractionPayload, ExtractionRecord, Span, WorkItem
from .field_schema import empty_contract_row, extraction_schema, flat_fields, native_field_paths, scope_paths, set_field
from .render import parse_source
from .sections import SectionIndex
from .spans import text_for
from .tiling import VisionLimits, tile_image


@dataclass
class SegmentContext:
    content: str
    item: WorkItem
    section_index: SectionIndex | None = None
    figures: dict[str, bytes] = field(default_factory=dict)
    fields: dict[str, ContentField] | None = None

    @property
    def text(self) -> str:
        return text_for(self.content, [span.as_tuple() for span in self.item.spans])


class ModelClient(Protocol):
    name: str

    def complete_json(self, *, prompt: str, schema: dict, images: list[bytes] | None = None) -> dict: ...


class Extractor(Protocol):
    content_type: ContentType

    def extract(self, context: SegmentContext) -> ExtractionPayload: ...


def plain(node: ContentField):
    value = node.value
    if isinstance(value, dict):
        return {name: plain(child) for name, child in value.items()}
    if isinstance(value, list):
        return [plain(child) for child in value]
    return value


def native_evidence(node: ContentField, context: SegmentContext) -> Evidence | None:
    page, polygon = parse_source(node.source)
    if page is None or not context.item.first_page <= page <= context.item.last_page:
        return None
    return Evidence(
        file_id=context.item.file_id, page=page, polygon=polygon,
        source="table" if context.item.content_type is ContentType.SCHEDULE else "text",
        spans=[Span(offset=span.offset, length=span.length) for span in node.spans or []],
        section_path=context.item.section_root, confidence=node.confidence,
        verbatim=str(plain(node)),
    )


def map_record(values: dict, context: SegmentContext) -> ExtractionRecord:
    allowed = extraction_schema()["properties"]["rows"]["items"]["properties"]
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(f"Incompatible extraction schema; redeploy analyzers. Unknown fields: {sorted(unknown)}")
    record = ExtractionRecord(
        row=empty_contract_row(), motor_tag=values.get("motor_tag"),
        identified_system=values.get("identified_system") is True,
        origin=context.item.content_type, sources=[context.item.segment_id],
    )
    for path in flat_fields():
        if values.get(path) is not None:
            set_field(record.row, path, values[path])
    return record


@dataclass
class RoutedFieldExtractor:
    content_type: ContentType

    def extract(self, context: SegmentContext) -> ExtractionPayload:
        if not context.fields or "rows" not in context.fields:
            raise ValueError("Native CU fields missing or incompatible; deploy the schema-derived field analyzer")
        payload = ExtractionPayload()
        paths = native_field_paths()
        for entry in context.fields["rows"].value_array or []:
            unknown = set(entry.value_object) - set(paths)
            if unknown:
                raise ValueError(f"Incompatible native field names; redeploy analyzers: {sorted(unknown)}")
            values = {paths[name]: plain(node) for name, node in entry.value_object.items()}
            record = map_record(values, context)
            for name, node in entry.value_object.items():
                path = paths[name]
                if path in flat_fields() or path == "motor_tag":
                    evidence = native_evidence(node, context)
                    if evidence:
                        record.evidence[path] = [evidence]
            scope_node = entry.value_object.get("all_vfds_scope")
            for scope in (scope_node.value_array or []) if scope_node else []:
                fields = scope.value_object
                path, quote = plain(fields["field"]), plain(fields["quote"])
                evidence = native_evidence(fields["quote"], context)
                if (context.item.content_type is ContentType.TEXT and not record.row["tag"]
                        and path in scope_paths() and quote and evidence
                        and " ".join(quote.split()) in " ".join(context.text.split())):
                    record.scopes[path] = evidence
                else:
                    record.issues.append("Unverified applicability clause; requirement not shared")
            payload.records.append(record)
        return payload


DROP_REASONS = {
    ContentType.PLAN: "floor plans and physical layouts are excluded",
}


@dataclass
class DropExtractor:
    content_type: ContentType
    reason: str

    def extract(self, context: SegmentContext) -> ExtractionPayload:
        return ExtractionPayload(notes=[f"{context.item.segment_id} not extracted: {self.reason}"])


@dataclass
class ModelExtractor:
    content_type: ContentType
    model: ModelClient
    vision_limits: VisionLimits = field(default_factory=VisionLimits.azure_openai)
    max_tiles: int = 40

    def extract(self, context: SegmentContext) -> ExtractionPayload:
        if self.content_type is not ContentType.DRAWING or not context.figures:
            raise ValueError("Drawing fallback requires rendered source regions")
        payload = ExtractionPayload()
        schema = extraction_schema()
        for figure_id, image in context.figures.items():
            page = int(figure_id.split("-")[1])
            tiles, _, _ = tile_image(image, self.vision_limits, max_tiles=self.max_tiles)
            if len(tiles) > 50:
                raise ValueError("Drawing exceeds the model image limit; no tiles were discarded")
            prompt = (
    "Extract the individually identified motor-control systems and their associated motors "
    "from this drawing region, regardless of controller type. "
    "Images are overlapping tiles of the same region: combine repeated sightings, "
    "but associate equipment only where labels or drawn connections support the relationship. "
    "Record the stated controller type in notes; do not treat different controller types "
    "as interchangeable. "
    "Use the supplied field descriptions. Populate a field only when its meaning applies "
    "to that component; retain other relevant specifications in notes. "
    "Preserve printed identifiers, values, units and qualifiers. Flag uncertain readings "
    "rather than inventing or silently correcting them. "
    "Keep controller, motor and upstream protective-device ratings separate. "
    "A minimum rating is not an actual operating value or motor full-load current. "
    "Do not create additional equipment from illustrative examples or repeated symbols alone. "
    "Leave unknown fields null or empty and explain unresolved connections in notes.\n"
    + context.text
)
            response = self.model.complete_json(prompt=prompt, schema=schema, images=[tile.png for tile in tiles])
            validate(response, schema)
            for values in response["rows"]:
                record = map_record(values, context)
                record.issues.append("Drawing model reading: verify values and connections against source image")
                record.evidence["drawing"] = [Evidence(
                    file_id=context.item.file_id, page=page, source="derived",
                    verbatim=record.row["notes"], section_path=context.item.section_root,
                )]
                payload.records.append(record)
        return payload


class NullModel:
    name = "disabled"

    def complete_json(self, **kwargs):
        raise ValueError("Drawing model is disabled; configure DRAWING_MODEL")


def default_extractors(model: ModelClient, limits: VisionLimits | None = None) -> dict[ContentType, Extractor]:
    return {
        ContentType.TEXT: RoutedFieldExtractor(ContentType.TEXT),
        ContentType.SCHEDULE: RoutedFieldExtractor(ContentType.SCHEDULE),
        ContentType.DRAWING: ModelExtractor(ContentType.DRAWING, model, limits or VisionLimits.azure_openai()),
        **{kind: DropExtractor(kind, reason) for kind, reason in DROP_REASONS.items()},
    }