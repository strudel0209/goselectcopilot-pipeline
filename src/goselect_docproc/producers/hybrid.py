"""Hybrid producer — Content Understanding routes, Document Intelligence splits.

Each service is used for what it is actually good at:

* **Content Understanding** classifies and splits at page level from a category
  *description*, with no training data. That is a far better router than a hand
  tuned heuristic.
* **Document Intelligence Layout** supplies the character spans, geometry and
  server side figure crops that intra page separation requires, and that
  Content Understanding does not expose.

The result keeps DI's content string as authoritative (span offsets must come
from one place) and overrides only the segment boundaries and labels.

Cost is the sum of both, so the bench off must justify it against the single
service producers rather than assuming a hybrid is better.
"""

from __future__ import annotations

import logging

from ..contracts import ContentType, Segment
from ..regions import page_regions
from .base import DocumentAnalysis, ProducerCapabilities, ProducerCost, register
from .content_understanding import ContentUnderstandingProducer
from .di_layout import DILayoutProducer

log = logging.getLogger(__name__)


@register("hybrid-cu-di")
class HybridCUProducer:
    capabilities = ProducerCapabilities(
        native_regions=True,
        native_figure_crops=True,
        intra_page=True,
        residency="azure-native",
    )

    def __init__(
        self,
        router: ContentUnderstandingProducer,
        geometry: DILayoutProducer,
    ) -> None:
        self.router = router
        self.geometry = geometry

    def analyze(self, file_id: str, data: bytes, source_uri: str | None = None) -> DocumentAnalysis:
        base = self.geometry.analyze(file_id, data)
        routed = self.router.analyze(file_id, data, source_uri)

        page_types: dict[int, tuple[ContentType, float]] = {
            page: (segment.content_type, segment.confidence)
            for segment in routed.segments
            for page in segment.pages
        }
        if not page_types:
            base.warnings.append("router returned no segments; fell back to DI labels")
            base.producer = self.name
            return base

        segments: list[Segment] = []
        for page in range(1, base.page_count + 1):
            kind, confidence = page_types.get(page, (ContentType.OTHER, 0.0))
            last = segments[-1] if segments else None
            if last and last.content_type == kind and last.last_page == page - 1:
                last.last_page = page
                last.page_confidences.append(confidence)
            else:
                segments.append(
                    Segment(
                        segment_id=f"{file_id}-seg-{len(segments) + 1:03d}",
                        file_id=file_id,
                        first_page=page,
                        last_page=page,
                        content_type=kind,
                        confidence=confidence,
                        page_confidences=[confidence],
                        producer=self.name,
                    )
                )

        figures: dict[str, list[str]] = {}
        for segment in segments:
            segment.confidence = round(min(segment.page_confidences), 3)
            # Claim order depends on the routed type, so regions are rebuilt here.
            segment.regions = sorted(
                (
                    region
                    for page in segment.pages
                    for region in page_regions(base.native, page, segment_type=segment.content_type)
                ),
                key=lambda r: r.start,
            )
            segment.section_root = base.section_index.root_for(
                segment.start, inherits=segment.content_type is not ContentType.DRAWING
            )
            figures[segment.segment_id] = [
                f.id
                for f in (getattr(base.native, "figures", None) or [])
                if any(
                    segment.first_page <= r.page_number <= segment.last_page
                    for r in (f.bounding_regions or [])
                )
            ]

        return DocumentAnalysis(
            file_id=file_id,
            content=base.content,
            page_count=base.page_count,
            content_sha256=base.content_sha256,
            segments=segments,
            section_index=base.section_index,
            producer=self.name,
            cost=base.cost + routed.cost,
            figure_ids_by_segment=figures,
            native=base.native,
            warnings=base.warnings + routed.warnings,
        )

    def figure_image(self, analysis: DocumentAnalysis, figure_id: str) -> bytes | None:
        return self.geometry.figure_image(analysis, figure_id)
