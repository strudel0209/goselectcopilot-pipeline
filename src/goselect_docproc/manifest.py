"""Manifest construction — the contract the orchestrator persists.

Its shape is **independent of which producer made the segments**. That is the
whole point: DI Layout today, Content Understanding tomorrow,
no downstream change.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .contracts import ContentType, FileRef, Manifest, Span, WorkItem

if TYPE_CHECKING:
    from .producers.base import DocumentAnalysis

REVIEW_THRESHOLD = 0.25


def build_manifest(
    analyses: list["DocumentAnalysis"],
    job_id: str | None = None,
    correlation_id: str | None = None,
) -> Manifest:
    """File ordinal is frozen here from list order and must never be re-derived:
    it is half of the global reassembly key."""
    refs: list[FileRef] = []
    segments = []
    nodes = []
    coverage = {}

    for ordinal, analysis in enumerate(analyses):
        segments.extend(analysis.segments)
        nodes.extend(analysis.section_index.nodes)
        coverage[analysis.file_id] = analysis.coverage()
        refs.append(
            FileRef(
                file_id=analysis.file_id,
                ordinal=ordinal,
                source_uri=analysis.source_uri or f"unknown://{analysis.file_id}",
                page_count=max(analysis.page_count, 1),
                content_sha256=analysis.content_sha256,
                content_chars=len(analysis.content),
            )
        )

    producers = sorted({a.producer for a in analyses})
    return Manifest(
        job_id=job_id or str(uuid.uuid4()),
        correlation_id=correlation_id or str(uuid.uuid4()),
        files=refs,
        segments=segments,
        section_index=sorted(nodes, key=lambda n: n.offset),
        coverage=coverage,
        producer="+".join(producers) if producers else "unknown",
    )


def work_items(
    manifest: Manifest,
    analyses: dict[str, "DocumentAnalysis"],
    layout_uri_template: str = "jobs/{job_id}/files/{file_id}/layout.json",
) -> list[WorkItem]:
    """One queue message per segment. Feature flags become per-segment here."""
    items: list[WorkItem] = []
    for segment in sorted(manifest.segments, key=manifest.sort_key):
        drawing = segment.content_type is ContentType.DRAWING
        analysis = analyses.get(segment.file_id)
        items.append(
            WorkItem(
                job_id=manifest.job_id,
                correlation_id=manifest.correlation_id,
                file_id=segment.file_id,
                file_ordinal=manifest.file(segment.file_id).ordinal,
                segment_id=segment.segment_id,
                content_type=segment.content_type,
                first_page=segment.first_page,
                last_page=segment.last_page,
                spans=[
                    Span(offset=s.offset, length=s.length)
                    for r in segment.regions
                    for s in r.spans
                ],
                section_root=segment.section_root,
                layout_uri=layout_uri_template.format(
                    job_id=manifest.job_id, file_id=segment.file_id
                ),
                figures=(
                    analysis.figure_ids_by_segment.get(segment.segment_id, [])
                    if analysis and drawing
                    else []
                ),
                high_resolution=drawing,
                formulas=False,  # never on drawings: boxed tags become \sqrt{}
            )
        )
    return items
