"""Swappable segment producers.

The spine is producer independent. Choose by measurement, not by argument:

    goselect-docproc bench --producers di-layout,content-understanding corpus/*.pdf
"""

from .base import (
    DocumentAnalysis,
    ProducerCapabilities,
    ProducerCost,
    SegmentProducer,
    available,
    get,
    register,
)
from .content_understanding import ROUTER_ANALYZER, ContentUnderstandingProducer
from .di_layout import DILayoutProducer

__all__ = [
    "ContentUnderstandingProducer",
    "DILayoutProducer",
    "DocumentAnalysis",
    "ProducerCapabilities",
    "ProducerCost",
    "ROUTER_ANALYZER",
    "SegmentProducer",
    "available",
    "get",
    "register",
]
