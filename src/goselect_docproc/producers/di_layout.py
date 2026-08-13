"""Document Intelligence Layout producer — the zero-new-dependency baseline.

Wraps the existing profiling, heuristic classification, span subtraction and
section indexing. Nothing about this file is novel; it exists so the DI path
competes on the same terms as every other producer in the bench-off.

Strengths: Azure-native, no residency question, server-side figure crops, no
page cap, geometry preserved for every element.

Weakness: page type is a **heuristic with no measured accuracy**. That is the
thing the bench-off is meant to settle.
"""

from __future__ import annotations

from typing import Any

from ..contracts import ContentType, Segment
from ..layout import LayoutClient, LayoutOptions
from ..regions import figure_ids, page_regions
from ..sections import build_section_index
from ..segmentation import HeuristicClassifier, PageClassifier, build_segments, profile_pages
from .base import DocumentAnalysis, ProducerCapabilities, ProducerCost, register

# Layout is billed per page; the S0 list price at time of writing.
USD_PER_PAGE = 0.010


@register("di-layout")
class DILayoutProducer:
    capabilities = ProducerCapabilities(
        native_regions=True,
        native_figure_crops=True,
        intra_page=True,
        residency="azure-native",
    )

    def __init__(
        self,
        layout: LayoutClient,
        classifier: PageClassifier | None = None,
        usd_per_page: float = USD_PER_PAGE,
    ) -> None:
        self.layout = layout
        self.classifier = classifier or HeuristicClassifier()
        self.usd_per_page = usd_per_page
        self._digests: dict[str, str] = {}

    def analyze(self, file_id: str, data: bytes, source_uri: str | None = None) -> DocumentAnalysis:
        result, digest = self.layout.analyze(data, LayoutOptions())
        self._digests[file_id] = digest

        index = build_section_index(result)
        labels = self.classifier.label(profile_pages(result))
        descriptors = build_segments(labels, file_id, self.classifier.producer)

        segments: list[Segment] = []
        figures: dict[str, list[str]] = {}
        for descriptor in descriptors:
            content_type = descriptor["content_type"]
            regions = [
                region
                for page in range(descriptor["first_page"], descriptor["last_page"] + 1)
                for region in page_regions(result, page, segment_type=content_type)
            ]
            segment = Segment(**descriptor, regions=sorted(regions, key=lambda r: r.start))
            segment.section_root = index.root_for(
                segment.start, inherits=content_type is not ContentType.DRAWING
            )
            segments.append(segment)
            figures[segment.segment_id] = figure_ids(
                result, segment.first_page, segment.last_page
            )

        return DocumentAnalysis(
            file_id=file_id,
            content=result.content,
            page_count=len(result.pages),
            content_sha256=digest,
            segments=segments,
            section_index=index,
            producer=self.name,
            cost=ProducerCost(
                pages=len(result.pages),
                api_calls=1,
                usd_estimate=round(len(result.pages) * self.usd_per_page, 6),
            ),
            figure_ids_by_segment=figures,
            native=result,
        )

    def figure_image(self, analysis: DocumentAnalysis, figure_id: str) -> bytes | None:
        digest = self._digests.get(analysis.file_id, analysis.content_sha256)
        return self.layout.figure_png(digest, figure_id)
