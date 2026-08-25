"""Swappable segment producers.

The spine is producer independent: a producer reduces a service's native model
to the neutral shapes in ``base``, and nothing downstream knows which one ran.
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
from .content_understanding import ContentUnderstandingProducer, router_analyzer

__all__ = [
    "ContentUnderstandingProducer",
    "DocumentAnalysis",
    "ProducerCapabilities",
    "ProducerCost",
    "router_analyzer",
    "SegmentProducer",
    "available",
    "get",
    "register",
]
