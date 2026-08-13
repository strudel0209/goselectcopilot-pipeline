"""Producer bench-off.

Turns "is this too complex?" into a measurement. The same corpus, the same
gates, every producer — so the decision is made by numbers rather than by
preference.

Two modes:

* **Structural** (no labels needed) — coverage, intra-page routing rate, section
  index quality, cost per page, wall time. Enough to disqualify a producer.
* **Scored** (labels required) — feeds ``eval/score.py`` gates per producer.

The structural metric that matters most for ABB is ``multi_kind_page_rate``: the
share of pages where the producer found more than one content kind. A producer
that cannot split inside a page reports 0.0 by construction, which on a corpus
where 15 of 20 pages are mixed is a disqualifying result, not a rounding error.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .contracts import ContentType
from .producers.base import DocumentAnalysis, SegmentProducer

log = logging.getLogger(__name__)


@dataclass
class BenchRow:
    producer: str
    document: str
    ok: bool = True
    error: str | None = None

    pages: int = 0
    segments: int = 0
    regions: int = 0
    multi_kind_pages: int = 0
    kinds_found: list[str] = field(default_factory=list)

    coverage_ratio: float = 0.0
    unexplained_chars: int = 0

    section_nodes: int = 0
    section_strategy: str = ""
    segments_with_breadcrumb: int = 0

    mean_confidence: float = 0.0
    review_segments: int = 0

    api_calls: int = 0
    usd_estimate: float = 0.0
    usd_per_page: float = 0.0
    seconds: float = 0.0

    warnings: list[str] = field(default_factory=list)

    @property
    def multi_kind_page_rate(self) -> float:
        return round(self.multi_kind_pages / self.pages, 3) if self.pages else 0.0

    @property
    def breadcrumb_rate(self) -> float:
        return round(self.segments_with_breadcrumb / self.segments, 3) if self.segments else 0.0


def measure(analysis: DocumentAnalysis, document: str, seconds: float) -> BenchRow:
    coverage = analysis.coverage()

    kinds_by_page: dict[int, set[ContentType]] = {}
    regions = 0
    for segment in analysis.segments:
        for region in segment.regions:
            regions += 1
            kinds_by_page.setdefault(region.page, set()).add(region.kind)

    confidences = [s.confidence for s in analysis.segments]
    return BenchRow(
        producer=analysis.producer,
        document=document,
        pages=analysis.page_count,
        segments=len(analysis.segments),
        regions=regions,
        multi_kind_pages=sum(1 for kinds in kinds_by_page.values() if len(kinds) > 1),
        kinds_found=sorted({k.value for kinds in kinds_by_page.values() for k in kinds}),
        coverage_ratio=round(coverage.accounted_ratio, 4),
        unexplained_chars=coverage.unexplained_chars,
        section_nodes=len(analysis.section_index.nodes),
        section_strategy=analysis.section_index.strategy,
        segments_with_breadcrumb=sum(1 for s in analysis.segments if s.section_root),
        mean_confidence=round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
        review_segments=sum(1 for c in confidences if c < 0.25),
        api_calls=analysis.cost.api_calls,
        usd_estimate=analysis.cost.usd_estimate,
        usd_per_page=analysis.cost.usd_per_page,
        seconds=round(seconds, 2),
        warnings=list(analysis.warnings),
    )


def run(
    producers: dict[str, SegmentProducer],
    documents: dict[str, bytes],
    source_uris: dict[str, str] | None = None,
) -> list[BenchRow]:
    rows: list[BenchRow] = []
    for name, producer in producers.items():
        for document, data in documents.items():
            started = time.perf_counter()
            try:
                analysis = producer.analyze("f1", data, (source_uris or {}).get(document))
                rows.append(measure(analysis, document, time.perf_counter() - started))
            except Exception as exc:  # noqa: BLE001 - a producer failing IS the result
                log.warning("%s failed on %s: %s", name, document, exc)
                rows.append(
                    BenchRow(
                        producer=name,
                        document=document,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        seconds=round(time.perf_counter() - started, 2),
                    )
                )
    return rows


def aggregate(rows: list[BenchRow]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for producer in sorted({r.producer for r in rows}):
        subset = [r for r in rows if r.producer == producer]
        ok = [r for r in subset if r.ok]
        summary[producer] = {
            "documents": len(subset),
            "failed": len(subset) - len(ok),
            "pages": sum(r.pages for r in ok),
            "coverage_ratio": round(sum(r.coverage_ratio for r in ok) / len(ok), 4) if ok else 0.0,
            "unexplained_chars": sum(r.unexplained_chars for r in ok),
            "multi_kind_page_rate": round(
                sum(r.multi_kind_pages for r in ok) / max(sum(r.pages for r in ok), 1), 3
            ),
            "breadcrumb_rate": round(
                sum(r.segments_with_breadcrumb for r in ok) / max(sum(r.segments for r in ok), 1), 3
            ),
            "review_rate": round(
                sum(r.review_segments for r in ok) / max(sum(r.segments for r in ok), 1), 3
            ),
            "usd_per_page": round(
                sum(r.usd_estimate for r in ok) / max(sum(r.pages for r in ok), 1), 6
            ),
            "seconds_per_page": round(
                sum(r.seconds for r in ok) / max(sum(r.pages for r in ok), 1), 3
            ),
            "warnings": sorted({w for r in subset for w in r.warnings}),
        }
    return summary


HEADERS = [
    ("documents", "docs", 5),
    ("failed", "fail", 5),
    ("pages", "pages", 6),
    ("coverage_ratio", "coverage", 9),
    ("unexplained_chars", "lost", 6),
    ("multi_kind_page_rate", "intra-page", 11),
    ("breadcrumb_rate", "breadcrumb", 11),
    ("review_rate", "review", 7),
    ("usd_per_page", "USD/page", 9),
    ("seconds_per_page", "s/page", 7),
]


def render(rows: list[BenchRow]) -> str:
    summary = aggregate(rows)
    width = max((len(p) for p in summary), default=10)

    lines = ["", "PRODUCER BENCH-OFF", ""]
    header = "producer".ljust(width) + "".join(label.rjust(w) for _, label, w in HEADERS)
    lines.append(header)
    lines.append("-" * len(header))
    for producer, values in summary.items():
        row = producer.ljust(width)
        for key, _, w in HEADERS:
            value = values[key]
            text = f"{value:.4f}" if isinstance(value, float) and value < 1 else str(value)
            row += text.rjust(w)
        lines.append(row)

    lines.append("")
    lines.append("intra-page = share of pages where more than one content kind was found.")
    lines.append("A producer that cannot split inside a page reports 0.000 by construction.")

    for producer, values in summary.items():
        if values["warnings"]:
            lines.append("")
            lines.append(f"{producer} constraints:")
            for warning in values["warnings"]:
                lines.append(f"  - {warning}")

    lost = {p: v for p, v in summary.items() if v["unexplained_chars"]}
    if lost:
        lines.append("")
        lines.append("CONTENT LOSS — disqualifying until explained:")
        for producer, values in lost.items():
            lines.append(f"  {producer}: {values['unexplained_chars']} chars")
    return "\n".join(lines)


def write(rows: list[BenchRow], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "bench.json"
    path.write_text(
        json.dumps(
            {"rows": [asdict(r) for r in rows], "summary": aggregate(rows)},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path
