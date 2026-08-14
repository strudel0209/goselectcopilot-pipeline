"""In-process orchestrator.

Deliberately a **reference implementation of the state machine**, not a
replacement for it. Each method maps 1:1 to a state in the existing Python
orchestrator, so the customer can adopt the logic without adopting the runtime:

======================================  ===========================================
This module                             GoSelect state
======================================  ===========================================
``Pipeline.segment``                    Segmentation_State  (new)
``Pipeline.plan``                       Enqueue to Service Bus
``Pipeline.run_segment``                Container Apps Job worker
``Pipeline.reconcile``                  CrossSegment_Reconciliation (new)
``Pipeline.finish``                     JSON_Validation_State + Save_Results
======================================  ===========================================

Concurrency here is a thread pool; in production it is queue depth. Nothing in
the logic depends on which.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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
from .reconcile import TagLexicon, harvest
from .render import render_pages
from .spans import text_for
from .validate import validate_payload

log = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    review_threshold: float = 0.25
    max_workers: int = 4
    max_attempts: int = 3
    require_grounding: bool = True
    # Measured on Plainville sheet 1 (E-size): 100 dpi reads VFD-401 and RWP-401
    # correctly in 24 tiles; 150 dpi reads the same tags but needs 48, over the
    # 40-tile cost guard. DI's own crop of that sheet is 1477x934 and reads VD-401.
    drawing_dpi: int = 100
    cache_dir: Path = Path(".cache")
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
        return manifest

    # -- Enqueue ------------------------------------------------------------

    def plan(self, manifest: Manifest) -> list[WorkItem]:
        return work_items(manifest, self._analyses)

    # -- Cross-segment prerequisites ---------------------------------------

    def build_lexicon(self, manifest: Manifest) -> TagLexicon:
        """Harvest authoritative tags from the whole package before extraction.

        Schedules are the cleanest source, but a drawings-only package has none -
        and on the Plainville sheet the layout model still reads 122 correct tags
        that the vision pass can be snapped back to. Restricting the harvest to
        SCHEDULE segments left that package with an empty lexicon and no repair.

        This is only possible because the package is one job. It is the reason
        drawing tag repair works at all.
        """
        texts: list[str] = []
        for segment in manifest.segments:
            content = self._analyses[segment.file_id].content
            texts.append(
                text_for(content, [s.as_tuple() for r in segment.regions for s in r.spans])
            )
        return TagLexicon(harvest(texts))

    # -- Worker -------------------------------------------------------------

    def run_segment(self, item: WorkItem, lexicon: TagLexicon | None = None) -> SegmentResult:
        extractor = self.extractors.get(item.content_type)
        if extractor is None:
            return SegmentResult.for_item(item, status=Status.DONE)

        analysis = self._analyses[item.file_id]
        figures = {}
        if item.content_type is ContentType.DRAWING:
            # Render from the source PDF rather than taking the service's crop:
            # DI's crops arrive pre-downsampled and CU returns no image at all.
            figures = render_pages(
                self._sources.get(item.file_id, b""),
                item.first_page,
                item.last_page,
                dpi=self.config.drawing_dpi,
            )
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
            lexicon=lexicon,
        )

        started = time.perf_counter()
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                payload = extractor.extract(context)
                errors = validate_payload(payload, require_grounding=self.config.require_grounding)
                return SegmentResult.for_item(
                    item,
                    status=Status.REVIEW if errors else Status.DONE,
                    payload=payload,
                    attempts=attempt,
                    errors=errors,
                    model=getattr(getattr(extractor, "model", None), "name", None),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001 - partial failure is recorded, not raised
                log.warning("segment %s attempt %d failed: %s", item.segment_id, attempt, exc)
                if attempt == self.config.max_attempts:
                    return SegmentResult.for_item(
                        item,
                        status=Status.FAILED,
                        attempts=attempt,
                        errors=[f"{type(exc).__name__}: {exc}"],
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                time.sleep(min(2**attempt * 0.1, 2.0))
        raise AssertionError("unreachable")

    # -- Fan out ------------------------------------------------------------

    def run_all(
        self,
        items: list[WorkItem],
        lexicon: TagLexicon | None = None,
        on_result: Callable[[SegmentResult], None] | None = None,
    ) -> list[SegmentResult]:
        results: list[SegmentResult] = []
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            for result in pool.map(lambda i: self.run_segment(i, lexicon), items):
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
        lexicon = self.build_lexicon(manifest)
        items = self.plan(manifest)
        results = self.run_all(items, lexicon)
        return manifest, self.finish(manifest, results), results

    def content_by_file(self) -> dict[str, str]:
        return {file_id: a.content for file_id, a in self._analyses.items()}

    def analyses(self) -> dict[str, DocumentAnalysis]:
        return dict(self._analyses)
