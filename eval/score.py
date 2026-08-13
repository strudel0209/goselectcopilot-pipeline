"""Scoring harness.

Nothing here is optional. A fix demonstrated on one document is a coincidence;
these are the numbers that decide whether the pipeline ships.

**This never runs in production.** Labels are exam papers, not pipeline inputs.
A production job has no label and never will: it runs, emits confidence,
grounding and a coverage proof, and routes what it is unsure about to review.
Scoring happens offline against a fixed, hand-labelled corpus - before go-live,
and again on every change that could move accuracy. The corpus grows for free
from corrections made in the review queue.

Only five keys are read: ``scan_quality``, ``pages``, ``sections``, ``tags``,
``pairs``. An optional sixth, ``headings``, lists the document's real headings so
the section index is scored on what it finds *and what it invents* - a drawing's
handwriting promoted to a clause is a precision failure, and the page-level
``sections`` check cannot see it. Anything else in a label file is documentation
for humans.

Label file - one JSON per document, in ``eval/labels/<name>.json``::

    {
      "document": "cust-pkg-001.pdf",
      "scan_quality": "digital",            // or "scanned" - reported separately
      "pages": {"1": "TEXT", "2": "SCHEDULE", "7": "DRAWING"},
      "sections": {"1": "3. Scope of supply", "7": "5.1 Motor schedule"},
      "tags": ["VFD-401", "M-401"],
      "pairs": [["VFD-401", "M-401"]]
    }

Run::

    python eval/score.py --labels eval/labels --manifests out/manifests

Acceptance gates are asserted, not eyeballed - see ``GATES``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ship gates. Boundary quality decides whether the plumbing is trustworthy;
# extraction quality decides whether the output is.
GATES = {
    "page_classification_f1": 0.85,
    "segment_boundary_iou": 0.90,
    "section_heading_f1": 0.90,
    "section_attribution_digital": 0.95,
    "section_attribution_scanned": 0.80,
    "coverage_pass_rate": 1.00,
    "pair_f1": 0.90,
}


def _norm(heading: str) -> str:
    return " ".join((heading or "").split()).upper()


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


@dataclass
class Report:
    per_class: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    pairs: Counts = field(default_factory=Counts)
    tags: Counts = field(default_factory=Counts)
    headings: Counts = field(default_factory=Counts)
    headings_labelled: int = 0
    boundary_iou: list[float] = field(default_factory=list)
    section_hits: dict[str, list[bool]] = field(default_factory=lambda: defaultdict(list))
    coverage_ok: list[bool] = field(default_factory=list)
    documents: int = 0

    def macro_f1(self) -> float:
        scores = [c.f1 for c in self.per_class.values()]
        return sum(scores) / len(scores) if scores else 0.0

    def mean_iou(self) -> float:
        return sum(self.boundary_iou) / len(self.boundary_iou) if self.boundary_iou else 0.0

    def section_accuracy(self, quality: str) -> float | None:
        hits = self.section_hits.get(quality, [])
        return sum(hits) / len(hits) if hits else None

    def coverage_rate(self) -> float:
        return sum(self.coverage_ok) / len(self.coverage_ok) if self.coverage_ok else 0.0


def _runs(page_types: dict[int, str]) -> list[tuple[int, int, str]]:
    runs: list[tuple[int, int, str]] = []
    for page in sorted(page_types):
        kind = page_types[page]
        if runs and runs[-1][2] == kind and runs[-1][1] == page - 1:
            runs[-1] = (runs[-1][0], page, kind)
        else:
            runs.append((page, page, kind))
    return runs


def _iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    overlap = max(0, hi - lo + 1)
    union = max(a[1], b[1]) - min(a[0], b[0]) + 1
    return overlap / union if union else 0.0


def score_document(label: dict[str, Any], manifest: dict[str, Any], report: Report) -> None:
    report.documents += 1
    quality = label.get("scan_quality", "digital")

    truth_pages = {int(k): v for k, v in label.get("pages", {}).items()}
    predicted_pages: dict[int, str] = {}
    for segment in manifest["segments"]:
        for page in range(segment["first_page"], segment["last_page"] + 1):
            predicted_pages[page] = segment["content_type"]

    for page, expected in truth_pages.items():
        actual = predicted_pages.get(page)
        if actual == expected:
            report.per_class[expected].tp += 1
        else:
            report.per_class[expected].fn += 1
            if actual:
                report.per_class[actual].fp += 1

    # Segment boundaries: best-matching predicted run per labelled run.
    predicted_runs = [
        (s["first_page"], s["last_page"], s["content_type"]) for s in manifest["segments"]
    ]
    for start, end, kind in _runs(truth_pages):
        best = max(
            (_iou((start, end), (p[0], p[1])) for p in predicted_runs if p[2] == kind),
            default=0.0,
        )
        report.boundary_iou.append(best)

    # Section attribution, reported separately for digital and scanned.
    index = manifest.get("section_index", [])
    offsets = sorted((n["offset"], n["heading"]) for n in index)

    # Heading detection: recall catches a missed clause, precision catches a
    # drawing's handwriting promoted to one.
    if "headings" in label:
        report.headings_labelled += 1
        truth = {_norm(h) for h in label["headings"]}
        found = {_norm(n["heading"]) for n in index}
        report.headings.tp += len(truth & found)
        report.headings.fp += len(found - truth)
        report.headings.fn += len(truth - found)

    for page_str, expected_heading in label.get("sections", {}).items():
        page = int(page_str)
        segment = next(
            (s for s in manifest["segments"] if s["first_page"] <= page <= s["last_page"]), None
        )
        actual = segment.get("section_root") if segment else None
        if actual is None and offsets:
            actual = offsets[0][1]
        report.section_hits[quality].append(
            bool(actual) and expected_heading.lower() in (actual or "").lower()
        )

    for coverage in manifest.get("coverage", {}).values():
        report.coverage_ok.append(coverage.get("unexplained_chars", 0) == 0)


def score_job(label: dict[str, Any], job: dict[str, Any], report: Report) -> None:
    payload = job.get("payload", {})

    truth_tags = {t.upper() for t in label.get("tags", [])}
    found_tags = {
        (s.get("tag") or "").upper()
        for s in payload.get("motors", []) + payload.get("vfds", [])
        if s.get("tag")
    }
    report.tags.tp += len(truth_tags & found_tags)
    report.tags.fp += len(found_tags - truth_tags)
    report.tags.fn += len(truth_tags - found_tags)

    truth_pairs = {(a.upper(), b.upper()) for a, b in label.get("pairs", [])}
    found_pairs = {
        ((p.get("vfd_tag") or "").upper(), (p.get("motor_tag") or "").upper())
        for p in payload.get("pairs", [])
    }
    report.pairs.tp += len(truth_pairs & found_pairs)
    report.pairs.fp += len(found_pairs - truth_pairs)
    report.pairs.fn += len(truth_pairs - found_pairs)


def render(report: Report) -> tuple[str, bool, dict[str, float | None]]:
    # None means "no labelled sample" and must never be scored as zero: a gate
    # that fails because nothing was measured hides the gates that really failed.
    measured: dict[str, float | None] = {
        "page_classification_f1": report.macro_f1(),
        "segment_boundary_iou": report.mean_iou(),
        "section_heading_f1": report.headings.f1 if report.headings_labelled else None,
        "section_attribution_digital": report.section_accuracy("digital"),
        "section_attribution_scanned": report.section_accuracy("scanned"),
        "coverage_pass_rate": report.coverage_rate(),
        "pair_f1": report.pairs.f1 if report.pairs.tp + report.pairs.fp + report.pairs.fn else None,
    }

    lines = [f"documents scored: {report.documents}", ""]
    lines.append("per-class page classification")
    for name, counts in sorted(report.per_class.items()):
        lines.append(
            f"  {name:9} P={counts.precision:.3f} R={counts.recall:.3f} "
            f"F1={counts.f1:.3f}  (tp={counts.tp} fp={counts.fp} fn={counts.fn})"
        )
    lines.append("")
    lines.append(f"heading detection P={report.headings.precision:.3f} R={report.headings.recall:.3f} F1={report.headings.f1:.3f}  (tp={report.headings.tp} fp={report.headings.fp} fn={report.headings.fn})")
    lines.append(f"tag extraction   P={report.tags.precision:.3f} R={report.tags.recall:.3f} F1={report.tags.f1:.3f}")
    lines.append(f"pair extraction  P={report.pairs.precision:.3f} R={report.pairs.recall:.3f} F1={report.pairs.f1:.3f}")
    lines.append("")
    lines.append("gates")

    passed = True
    unmeasured: list[str] = []
    for name, gate in GATES.items():
        value = measured[name]
        if value is None:
            unmeasured.append(name)
            lines.append(f"  {name:32} {'n/a':>5}  >= {gate:.2f}  [NOT MEASURED]")
            continue
        ok = value >= gate
        passed &= ok
        lines.append(f"  {name:32} {value:.3f} >= {gate:.2f}  [{'PASS' if ok else 'FAIL'}]")

    if unmeasured:
        lines.append("")
        lines.append(
            "  NOT MEASURED is not a pass. These gates have no labelled sample "
            "yet and block release just as a failure does:"
        )
        for name in unmeasured:
            lines.append(f"    - {name}")
        passed = False

    if not report.section_hits.get("scanned"):
        lines.append("")
        lines.append(
            "  WARNING: no scanned documents in the eval set. Scanned is where "
            "section attribution degrades; a digital-only score is not evidence."
        )
    return "\n".join(lines), passed, measured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score segmentation and extraction against labels")
    parser.add_argument("--labels", default="eval/labels", type=Path)
    parser.add_argument("--manifests", default="out/eval/manifests", type=Path)
    parser.add_argument("--jobs", default="out/eval/jobs", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("out/eval/scorecard.json"),
        help="Where to persist the scorecard. A run nobody can diff is not evidence.",
    )
    args = parser.parse_args(argv)

    report = Report()
    label_files = sorted(args.labels.glob("*.json"))
    if not label_files:
        print(f"no labels in {args.labels}; hand-label 15-20 real packages first", file=sys.stderr)
        return 2

    scored: list[str] = []
    for label_file in label_files:
        label = json.loads(label_file.read_text(encoding="utf-8"))
        stem = label_file.stem

        manifest_file = args.manifests / f"{stem}.json"
        if manifest_file.exists():
            score_document(label, json.loads(manifest_file.read_text(encoding="utf-8")), report)
            scored.append(stem)
        else:
            print(f"missing manifest for {stem}", file=sys.stderr)

        job_file = args.jobs / f"{stem}.json"
        if job_file.exists():
            score_job(label, json.loads(job_file.read_text(encoding="utf-8")), report)

    text, passed, measured = render(report)
    print(text)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "documents": scored,
                "gates": GATES,
                "measured": measured,
                "passed": passed,
                "per_class": {
                    k: {"tp": c.tp, "fp": c.fp, "fn": c.fn, "f1": round(c.f1, 4)}
                    for k, c in sorted(report.per_class.items())
                },
                "report": text,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nscorecard written to {args.out}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
