"""Integration surface for the GoSelect Copilot document pipeline.

Everything crossing a process, queue or service boundary is defined here. The
orchestrator, the workers and GoSelect all speak these models and nothing else.

Two layers on purpose:

* ``*Raw`` models are what an LLM is asked to return - flat, shallow, every
  field required and explicitly nullable. This is the shape that survives both
  OpenAI ``response_format`` and Claude forced-tool-use without dropping nested
  or null fields.
* Domain models are what GoSelect consumes. Expansion from raw to domain is
  deterministic Python, never a model call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0.0"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, populate_by_name=True)


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


class ContentType(StrEnum):
    TEXT = "TEXT"
    SCHEDULE = "SCHEDULE"
    DRAWING = "DRAWING"
    OTHER = "OTHER"


class Route(StrEnum):
    AUTO = "AUTO"
    REVIEW = "REVIEW"


class Status(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    REVIEW = "REVIEW"

    @property
    def terminal(self) -> bool:
        return self in {Status.DONE, Status.FAILED, Status.REVIEW}


class Span(Strict):
    """A character range in the single immutable ``content`` string of a file."""

    offset: int = Field(ge=0)
    length: int = Field(ge=0)

    @property
    def end(self) -> int:
        return self.offset + self.length

    def as_tuple(self) -> tuple[int, int]:
        return self.offset, self.length


Polygon = Annotated[list[float], Field(min_length=8)]


class Evidence(Strict):
    """Where a value came from. A value without evidence is rejected, not trusted."""

    file_id: str
    page: int = Field(ge=1)
    spans: list[Span] = Field(default_factory=list)
    polygon: Polygon | None = None
    section_path: str | None = None
    verbatim: str | None = None
    source: Literal["text", "table", "figure", "derived"] = "text"

    @property
    def start(self) -> int:
        return min((s.offset for s in self.spans), default=0)


# ---------------------------------------------------------------------------
# segmentation
# ---------------------------------------------------------------------------


class Region(Strict):
    """A typed slice of one page, produced by span subtraction."""

    kind: ContentType
    ref: str
    page: int = Field(ge=1)
    spans: list[Span]
    polygon: Polygon | None = None
    rows: int | None = None
    columns: int | None = None
    absorbed_tables: list[int] = Field(default_factory=list)

    @property
    def start(self) -> int:
        return min((s.offset for s in self.spans), default=0)

    @property
    def char_count(self) -> int:
        return sum(s.length for s in self.spans)


class SectionNode(Strict):
    offset: int = Field(ge=0)
    heading: str
    page: int = Field(ge=1)
    level: int = Field(ge=1)


class Segment(Strict):
    """The unit of work. Replaces the file as the thing the pipeline iterates."""

    segment_id: str
    file_id: str
    first_page: int = Field(ge=1)
    last_page: int = Field(ge=1)
    content_type: ContentType
    confidence: float = Field(ge=0.0, le=1.0)
    page_confidences: list[float] = Field(default_factory=list)
    section_root: str | None = None
    regions: list[Region] = Field(default_factory=list)
    producer: str = "unknown"

    @model_validator(mode="after")
    def _page_order(self) -> Segment:
        if self.last_page < self.first_page:
            raise ValueError("last_page precedes first_page")
        return self

    @property
    def pages(self) -> list[int]:
        return list(range(self.first_page, self.last_page + 1))

    @property
    def start(self) -> int:
        return min((r.start for r in self.regions), default=0)

    def route(self, threshold: float) -> Route:
        return Route.AUTO if self.confidence >= threshold else Route.REVIEW


class FileRef(Strict):
    file_id: str
    ordinal: int = Field(ge=0, description="Frozen at job creation; orders files globally")
    source_uri: str
    page_count: int = Field(ge=1)
    content_sha256: str
    content_chars: int = Field(ge=0)


class Coverage(Strict):
    """Proof that segmentation did not silently drop content."""

    total_chars: int
    covered_chars: int
    furniture_chars: int
    unexplained_chars: int
    unexplained_samples: list[str] = Field(default_factory=list)

    @property
    def accounted_ratio(self) -> float:
        # An empty analysis is a failure, not perfect coverage.
        if not self.total_chars:
            return 0.0
        return (self.covered_chars + self.furniture_chars) / self.total_chars

    @property
    def ok(self) -> bool:
        return self.unexplained_chars == 0


class Manifest(Strict):
    """The contract the orchestrator persists and the workers iterate."""

    schema_version: str = SCHEMA_VERSION
    job_id: str
    correlation_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    files: list[FileRef]
    segments: list[Segment]
    section_index: list[SectionNode] = Field(default_factory=list)
    coverage: dict[str, Coverage] = Field(default_factory=dict)
    producer: str = "di-layout-heuristic-v1"

    @property
    def expected_units(self) -> int:
        return len(self.segments)

    def file(self, file_id: str) -> FileRef:
        return next(f for f in self.files if f.file_id == file_id)

    def sort_key(self, segment: Segment) -> tuple[int, int]:
        """Global document order: file ordinal, then span offset. Never page number."""
        return self.file(segment.file_id).ordinal, segment.start


# ---------------------------------------------------------------------------
# queue message and worker result
# ---------------------------------------------------------------------------


class WorkItem(Strict):
    """Service Bus message body. A pointer, never a payload."""

    schema_version: str = SCHEMA_VERSION
    job_id: str
    correlation_id: str
    file_id: str
    file_ordinal: int
    segment_id: str
    content_type: ContentType
    first_page: int
    last_page: int
    spans: list[Span]
    section_root: str | None = None
    layout_uri: str
    figures: list[str] = Field(default_factory=list, description="DI figure ids on this segment")
    high_resolution: bool = False
    formulas: bool = False

    @property
    def dedupe_id(self) -> str:
        return f"{self.job_id}:{self.file_id}:{self.segment_id}"


class SegmentResult(Strict):
    """One Cosmos document. ``id`` is deterministic so redelivery is an upsert."""

    schema_version: str = SCHEMA_VERSION
    id: str
    job_id: str
    correlation_id: str
    file_id: str
    file_ordinal: int
    segment_id: str
    content_type: ContentType
    status: Status
    start_offset: int = 0
    attempts: int = 1
    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    payload: ExtractionPayload | None = None
    errors: list[str] = Field(default_factory=list)

    @classmethod
    def for_item(cls, item: WorkItem, **kw: Any) -> SegmentResult:
        return cls(
            id=item.dedupe_id,
            job_id=item.job_id,
            correlation_id=item.correlation_id,
            file_id=item.file_id,
            file_ordinal=item.file_ordinal,
            segment_id=item.segment_id,
            content_type=item.content_type,
            start_offset=min((s.offset for s in item.spans), default=0),
            **kw,
        )


# ---------------------------------------------------------------------------
# domain payload - what GoSelect consumes
# ---------------------------------------------------------------------------


class Quantity(Strict):
    value: float | None = None
    unit: str | None = None
    raw: str | None = None


class MotorSpec(Strict):
    tag: str | None = None
    power: Quantity = Field(default_factory=Quantity)
    voltage: Quantity = Field(default_factory=Quantity)
    frequency: Quantity = Field(default_factory=Quantity)
    speed: Quantity = Field(default_factory=Quantity)
    poles: int | None = None
    frame_size: str | None = None
    mounting: str | None = None
    ingress_protection: str | None = None
    insulation_class: str | None = None
    efficiency_class: str | None = None
    cooling: str | None = None
    hazardous_area: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class VfdSpec(Strict):
    tag: str | None = None
    power: Quantity = Field(default_factory=Quantity)
    voltage: Quantity = Field(default_factory=Quantity)
    current: Quantity = Field(default_factory=Quantity)
    enclosure: str | None = None
    control_mode: str | None = None
    filter: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class ApplicationSpec(Strict):
    process: str | None = None
    load_type: str | None = None
    duty_cycle: str | None = None
    ambient_temperature: Quantity = Field(default_factory=Quantity)
    altitude: Quantity = Field(default_factory=Quantity)
    hazardous_area: str | None = None
    standards: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class Pair(Strict):
    """A VFD-motor pairing. ``origin`` records which segment type asserted it."""

    pair_id: str
    vfd_tag: str | None = None
    motor_tag: str | None = None
    origin: ContentType
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    evidence: list[Evidence] = Field(default_factory=list)

    @property
    def key(self) -> tuple[str | None, str | None]:
        return self.vfd_tag, self.motor_tag


class ExtractionPayload(Strict):
    motors: list[MotorSpec] = Field(default_factory=list)
    vfds: list[VfdSpec] = Field(default_factory=list)
    applications: list[ApplicationSpec] = Field(default_factory=list)
    pairs: list[Pair] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Conflict(Strict):
    field: str
    values: list[str]
    origins: list[ContentType]
    resolution: str | None = None


class JobResult(Strict):
    """Final artefact handed to GoSelect."""

    schema_version: str = SCHEMA_VERSION
    job_id: str
    correlation_id: str
    status: Status
    segments_expected: int
    segments_done: int
    segments_failed: int
    payload: ExtractionPayload
    conflicts: list[Conflict] = Field(default_factory=list)
    review_required: list[str] = Field(default_factory=list)
    coverage: dict[str, Coverage] = Field(default_factory=dict)
    completed_at: datetime | None = None

    @property
    def partial(self) -> bool:
        return self.segments_failed > 0 or self.segments_done < self.segments_expected


SegmentResult.model_rebuild()
