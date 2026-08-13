"""Page profiling, classification and segment building.

The classifier is a **pluggable strategy**. The heuristic below exists to prove
the shape and to bootstrap labels cheaply; it is explicitly not production
accuracy. Swap in ``ContentUnderstandingClassifier`` or a DI custom classifier
and nothing downstream changes, because the contract is ``PageLabel``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from . import geometry as geo
from .contracts import ContentType
from .spans import overlaps

HEURISTIC_PRODUCER = "di-layout-heuristic-v1"

# Word density is measured per square inch. Calibrated on the Howey submittal:
# scanned Letter specification prose runs 3.7-5.8, a near-empty closing page 1.0,
# and E-size drawing sheets scaled to Letter run 3.3-5.8 but are dominated by
# figure area and rotated text instead.
DENSE_PROSE_WORDS_PER_SQIN = 5.0
SPARSE_SHEET_WORDS_PER_SQIN = 2.5
# Document Intelligence reports inches for PDFs and pixels for images.
PIXELS_PER_INCH = 96.0


@dataclass(frozen=True)
class PageProfile:
    page_number: int
    width: float
    height: float
    unit: str
    angle: float
    word_count: int
    line_count: int
    table_count: int
    figure_count: int
    selection_mark_count: int
    table_area_ratio: float
    figure_area_ratio: float
    word_density: float
    rotated_word_ratio: float
    short_token_ratio: float
    landscape: bool
    handwritten: bool


@dataclass(frozen=True)
class PageLabel:
    page_number: int
    content_type: ContentType
    confidence: float
    scores: dict[str, float]


@runtime_checkable
class PageClassifier(Protocol):
    producer: str

    def label(self, profiles: list[PageProfile]) -> list[PageLabel]: ...


def profile_pages(result: Any) -> list[PageProfile]:
    handwritten_spans = [
        (s.offset, s.length)
        for style in (result.styles or [])
        if getattr(style, "is_handwritten", False)
        for s in (style.spans or [])
    ]

    profiles: list[PageProfile] = []
    for page in result.pages:
        page_area = (page.width or 1) * (page.height or 1)
        # Density thresholds are per square inch, so pixel-unit pages must convert.
        area_sqin = page_area
        if "pixel" in str(getattr(page, "unit", "")).lower():
            area_sqin = page_area / (PIXELS_PER_INCH**2)
        words = list(page.words or [])
        lines = list(page.lines or [])

        tables = [t for t in (result.tables or []) if geo.on_page(t, page.page_number)]
        figures = [f for f in (result.figures or []) if geo.on_page(f, page.page_number)]

        table_area = sum(
            geo.area(r.polygon) for t in tables for r in geo.regions_on_page(t, page.page_number)
        )
        figure_area = sum(
            geo.area(r.polygon) for f in figures for r in geo.regions_on_page(f, page.page_number)
        )

        rotated = sum(1 for w in words if not geo.is_axis_aligned(w.polygon))
        short = sum(1 for w in words if len(w.content) <= 6)
        page_spans = [(s.offset, s.length) for ln in lines for s in (ln.spans or [])]

        profiles.append(
            PageProfile(
                page_number=page.page_number,
                width=page.width,
                height=page.height,
                unit=page.unit,
                angle=page.angle or 0.0,
                word_count=len(words),
                line_count=len(lines),
                table_count=len(tables),
                figure_count=len(figures),
                selection_mark_count=len(page.selection_marks or []),
                table_area_ratio=round(min(table_area / page_area, 1.0), 3),
                figure_area_ratio=round(min(figure_area / page_area, 1.0), 3),
                word_density=round(len(words) / area_sqin, 1),
                rotated_word_ratio=round(rotated / len(words), 3) if words else 0.0,
                short_token_ratio=round(short / len(words), 3) if words else 0.0,
                landscape=bool(page.width and page.height and page.width > page.height),
                handwritten=overlaps(handwritten_spans, page_spans),
            )
        )
    return profiles


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


class HeuristicClassifier:
    """Baseline only. Weights are tuned for inch-unit A/Letter pages.

    Retune on a labelled sample before quoting any accuracy number, or replace
    this class entirely - the ``PageLabel`` contract is what matters.
    """

    producer = HEURISTIC_PRODUCER

    def score(self, p: PageProfile) -> dict[str, float]:
        s = {"TEXT": 0.0, "SCHEDULE": 0.0, "DRAWING": 0.0, "OTHER": 0.25}

        # Drawings: large figure, landscape sheet, sparse rotated short tokens.
        s["DRAWING"] += 1.4 * _clamp(p.figure_area_ratio / 0.25)
        s["DRAWING"] += 0.5 if p.landscape else 0.0
        s["DRAWING"] += 0.8 * _clamp(
            (SPARSE_SHEET_WORDS_PER_SQIN - p.word_density) / SPARSE_SHEET_WORDS_PER_SQIN
        )
        s["DRAWING"] += 0.8 * _clamp(p.rotated_word_ratio / 0.05)
        s["DRAWING"] += 0.4 * _clamp((p.short_token_ratio - 0.55) / 0.45)

        # Schedules: page dominated by table area.
        s["SCHEDULE"] += 1.8 * _clamp(p.table_area_ratio / 0.30)
        s["SCHEDULE"] += 0.3 if p.table_count else 0.0
        s["SCHEDULE"] -= 0.6 * _clamp(p.figure_area_ratio / 0.25)

        # Specifications: dense prose, few tables, no dominant figure.
        s["TEXT"] += 1.5 * _clamp(p.word_density / DENSE_PROSE_WORDS_PER_SQIN)
        s["TEXT"] += 0.5 * (1.0 - _clamp(p.table_area_ratio / 0.30))
        s["TEXT"] -= 1.0 * _clamp(p.figure_area_ratio / 0.25)

        if p.word_count < 15:
            s["OTHER"] += 1.0
        return s

    def label(self, profiles: list[PageProfile]) -> list[PageLabel]:
        out: list[PageLabel] = []
        for p in profiles:
            scores = self.score(p)
            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            (top_name, top_score), (_, runner_up) = ranked[0], ranked[1]
            margin = (top_score - runner_up) / top_score if top_score > 0 else 0.0
            out.append(
                PageLabel(
                    page_number=p.page_number,
                    content_type=ContentType(top_name),
                    confidence=round(_clamp(margin), 3),
                    scores={k: round(v, 3) for k, v in scores.items()},
                )
            )
        return out


def build_segments(
    labels: list[PageLabel], file_id: str, producer: str = HEURISTIC_PRODUCER
) -> list[dict]:
    """Collapse contiguous same-type pages into segment descriptors.

    Segment confidence is the **minimum** page confidence in the run: a segment
    is only as trustworthy as its weakest page.
    """
    segments: list[dict] = []
    for label in sorted(labels, key=lambda x: x.page_number):
        last = segments[-1] if segments else None
        if last and last["content_type"] == label.content_type and last["last_page"] == label.page_number - 1:
            last["last_page"] = label.page_number
            last["page_confidences"].append(label.confidence)
        else:
            segments.append(
                {
                    "segment_id": f"{file_id}-seg-{len(segments) + 1:03d}",
                    "file_id": file_id,
                    "first_page": label.page_number,
                    "last_page": label.page_number,
                    "content_type": label.content_type,
                    "page_confidences": [label.confidence],
                    "producer": producer,
                }
            )

    for segment in segments:
        segment["confidence"] = round(min(segment["page_confidences"]), 3)
    return segments
