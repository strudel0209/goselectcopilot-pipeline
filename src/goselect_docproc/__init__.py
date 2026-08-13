"""Segment-level document processing for the ABB GoSelect Copilot pipeline.

The unit of work is the **segment**, not the file. Everything else follows.
"""

from .contracts import (
    ContentType,
    Coverage,
    Evidence,
    ExtractionPayload,
    JobResult,
    Manifest,
    Region,
    Segment,
    SegmentResult,
    Span,
    Status,
    WorkItem,
)
from .pipeline import Pipeline, PipelineConfig

__version__ = "0.1.0"

__all__ = [
    "ContentType",
    "Coverage",
    "Evidence",
    "ExtractionPayload",
    "JobResult",
    "Manifest",
    "Pipeline",
    "PipelineConfig",
    "Region",
    "Segment",
    "SegmentResult",
    "Span",
    "Status",
    "WorkItem",
    "__version__",
]
