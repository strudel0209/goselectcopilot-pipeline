"""Analyze, plan segment work, extract with evidence gates, and assemble once."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .assemble import merge
from .contracts import (
    ContentType,
    JobResult,
    Manifest,
    SegmentResult,
    Status,
    WorkItem,
)
from .extractors import Extractor, SegmentContext
from .manifest import build_manifest, work_items
from .producers.base import DocumentAnalysis, SegmentProducer
from .render import DEFAULT_DPI, render_pages
from .validate import validate_payload

log = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    review_threshold: float = 0.25
    field_review_threshold: float | None = None
    max_workers: int = 4
    require_grounding: bool = True
    drawing_dpi: int = DEFAULT_DPI
    output_dir: Path = Path("out")


@dataclass
class Pipeline:
    """The spine. Everything here is producer-independent by construction."""

    producer: SegmentProducer
    extractors: dict[ContentType, Extractor]
    config: PipelineConfig = field(default_factory=PipelineConfig)

    _analyses: dict[str, DocumentAnalysis] = field(default_factory=dict, init=False)
    _sources: dict[str, bytes] = field(default_factory=dict, init=False)

    # -- Segmentation_State -------------------------------------------------

    def segment(self, sources: dict[str, tuple[bytes, str]]) -> Manifest:
        """``sources`` maps ``file_id -> (pdf_bytes, source_uri)``."""
        analyses: list[DocumentAnalysis] = []
        for file_id, (data, source_uri) in sources.items():
            analysis = self.producer.analyze(file_id, data, source_uri)
            analysis.source_uri = source_uri
            self._analyses[file_id] = analysis
            self._sources[file_id] = data
            analyses.append(analysis)
            for warning in analysis.warnings:
                log.warning("%s [%s]: %s", file_id, analysis.producer, warning)

        manifest = build_manifest(analyses)
        for file_id, coverage in manifest.coverage.items():
            if not coverage.ok:
                log.warning(
                    "%s: %d unexplained chars dropped by segmentation; samples=%s",
                    file_id,
                    coverage.unexplained_chars,
                    coverage.unexplained_samples[:2],
                )
        self._warn_on_collateral_drops(manifest)
        return manifest

    def dropped_types(self) -> set[ContentType]:
        from .extractors import DropExtractor

        return {ct for ct, ex in self.extractors.items() if isinstance(ex, DropExtractor)}

    def _warn_on_collateral_drops(self, manifest: Manifest) -> None:
        """Policy drops an object, not a page. When a page-level classifier makes
        the segment the whole page, the schedule printed beside the plan goes
        with it - content loss by configuration, so it is reported not discovered.

        A plan's own body is a figure and types as a DRAWING region; that is the
        thing being dropped. Only prose and grids are collateral.
        """
        collateral_kinds = {ContentType.TEXT, ContentType.SCHEDULE}
        dropped = self.dropped_types()
        for segment in manifest.segments:
            if segment.content_type not in dropped:
                continue
            collateral = sorted(
                {r.kind.value for r in segment.regions if r.kind in collateral_kinds}
            )
            if collateral:
                log.warning(
                    "%s: dropping %s takes %s regions with it on pages %d-%d; "
                    "enable in-page segmentation to keep them",
                    segment.segment_id,
                    segment.content_type.value,
                    ",".join(collateral),
                    segment.first_page,
                    segment.last_page,
                )

    # -- Enqueue ------------------------------------------------------------

    def plan(self, manifest: Manifest) -> list[WorkItem]:
        missing = [file_id for file_id, coverage in manifest.coverage.items() if not coverage.ok]
        if missing:
            raise ValueError(f"Unassigned markdown or empty analysis in files: {', '.join(missing)}")
        return work_items(manifest, self._analyses)

    # -- Worker -------------------------------------------------------------

    def run_segment(self, item: WorkItem) -> SegmentResult:
        extractor = self.extractors.get(item.content_type)
        if extractor is None:
            return SegmentResult.for_item(
                item, status=Status.REVIEW, errors=[f"No extractor for {item.content_type.value}"],
            )

        analysis = self._analyses[item.file_id]
        figures = {}
        if item.content_type is ContentType.DRAWING:
            try:
                figures = render_pages(
                    self._sources[item.file_id], item.first_page, item.last_page,
                    dpi=self.config.drawing_dpi, source=item.source, unit=item.source_unit,
                )
            except Exception as exc:
                return SegmentResult.for_item(item, status=Status.FAILED, errors=[f"Drawing render: {exc}"])
        if not figures:
            for figure_id in item.figures:
                blob = self.producer.figure_image(analysis, figure_id)
                if blob:
                    figures[figure_id] = blob

        context = SegmentContext(
            content=analysis.content,
            item=item,
            section_index=analysis.section_index,
            figures=figures,
            fields=analysis.fields_by_segment.get(item.segment_id),
        )

        started = time.perf_counter()
        try:
            payload = extractor.extract(context)
            errors = validate_payload(payload, require_grounding=self.config.require_grounding,
                                      field_review_threshold=self.config.field_review_threshold)
            if not payload.records and item.content_type not in self.dropped_types():
                errors.append("No extracted records; inspect the source before treating this segment as irrelevant")
            return SegmentResult.for_item(
                item, status=Status.REVIEW if errors else Status.DONE, payload=payload, errors=errors,
                model=getattr(getattr(extractor, "model", None), "name", None),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            log.warning("segment %s failed: %s", item.segment_id, exc)
            return SegmentResult.for_item(item, status=Status.FAILED, errors=[f"{type(exc).__name__}: {exc}"],
                                          latency_ms=int((time.perf_counter() - started) * 1000))

    # -- Fan out ------------------------------------------------------------

    def run_all(
        self,
        items: list[WorkItem],
        on_result: Callable[[SegmentResult], None] | None = None,
    ) -> list[SegmentResult]:
        results: list[SegmentResult] = []
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            for result in pool.map(self.run_segment, items):
                results.append(result)
                if on_result:
                    on_result(result)
        return results

    # -- Reconciliation + Save_Results --------------------------------------

    def finish(self, manifest: Manifest, results: list[SegmentResult]) -> JobResult:
        return merge(manifest, results, review_threshold=self.config.review_threshold)

    # -- Convenience --------------------------------------------------------

    def run(self, sources: dict[str, tuple[bytes, str]]) -> tuple[Manifest, JobResult, list[SegmentResult]]:
        manifest = self.segment(sources)
        items = self.plan(manifest)
        results = self.run_all(items)
        return manifest, self.finish(manifest, results), results

    def content_by_file(self) -> dict[str, str]:
        return {file_id: a.content for file_id, a in self._analyses.items()}

    def analyses(self) -> dict[str, DocumentAnalysis]:
        return dict(self._analyses)
