"""Does in-page segmentation beat span subtraction?

The GA classifier's minimum unit is a page, which is why the old span-subtraction
path existed. ``2026-06-01-preview`` adds ``allow_in_page_segments``. This runs
both against the same documents and reports the difference; the measurement is
what justified deleting that path.

Two analyzers are deployed, identical except for that one flag, and neither
routes fields: this measures boundaries, not extraction.

    python eval/inpage_bench.py sample_docs/98813_2_VFDDrawingsNewmarketNHWTF.pdf

Content Understanding is not deterministic and nothing caches it, so ``--runs``
repeats each configuration and reports how often the answer changed.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parent.parent / ".env"))

from goselect_docproc.cli import _content_understanding_client  # noqa: E402
from goselect_docproc.contracts import ContentType  # noqa: E402
from goselect_docproc.producers.content_understanding import (  # noqa: E402
    ContentUnderstandingProducer,
    ensure_analyzer,
    router_analyzer,
)

PAGE_ANALYZER = "goselectBenchPage"
IN_PAGE_ANALYZER = "goselectBenchInPage"

# The measure that matters. Counting text *inside* a plan segment does not work:
# a plan sheet always carries dimension notes, so that number can never reach
# zero and improvement is invisible. What counts is a page where a grid or prose
# block was separated from the sheet it shares - at page granularity it is
# swallowed by the sheet, and if the sheet is a plan it is dropped with it.
SHEET = {ContentType.PLAN, ContentType.DRAWING}
DATA = {ContentType.SCHEDULE, ContentType.TEXT}


def rescued_pages(segments) -> list[int]:
    on_page: dict[int, set[ContentType]] = {}
    for segment in segments:
        for page in range(segment.first_page, segment.last_page + 1):
            on_page.setdefault(page, set()).add(segment.content_type)
    return sorted(p for p, kinds in on_page.items() if kinds & SHEET and kinds & DATA)


def observe(producer: ContentUnderstandingProducer, path: Path) -> dict:
    analysis = producer.analyze("f1", path.read_bytes(), f"file://{path.resolve()}")
    coverage = analysis.coverage()
    page_spans = {
        (s.first_page, s.last_page): sum(r.char_count for r in s.regions)
        for s in analysis.segments
    }
    sub_page = sum(
        1
        for s in analysis.segments
        if s.first_page == s.last_page
        and sum(1 for o in analysis.segments if o.first_page == s.first_page) > 1
    )
    rescued = rescued_pages(analysis.segments)
    return {
        "segments": len(analysis.segments),
        "sub_page_segments": sub_page,
        "kinds": [s.content_type.value for s in analysis.segments],
        "pages": [f"{s.first_page}-{s.last_page}" for s in analysis.segments],
        "confidence": [round(s.confidence, 3) for s in analysis.segments],
        "rescued_pages": rescued,
        "unexplained_chars": coverage.unexplained_chars,
        "accounted": round(coverage.accounted_ratio, 4),
        "signature": "|".join(
            f"{s.content_type.value}:{s.first_page}-{s.last_page}" for s in analysis.segments
        ),
        "chars": sum(page_spans.values()),
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    runs = int(next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--runs=")), "1"))
    pdfs = [Path(p) for p in args] or sorted(Path("sample_docs").glob("*.pdf"))

    client = _content_understanding_client()
    model = os.getenv("REPORT_MODEL", "gpt-5.4-mini")
    for analyzer_id, in_page in ((PAGE_ANALYZER, False), (IN_PAGE_ANALYZER, True)):
        ensure_analyzer(client, analyzer_id, router_analyzer(model, in_page_segments=in_page))
        print(f"analyzer {analyzer_id} ready (allow_in_page_segments={in_page})")

    rows: list[dict] = []
    for path in pdfs:
        for analyzer_id in (PAGE_ANALYZER, IN_PAGE_ANALYZER):
            producer = ContentUnderstandingProducer(client, analyzer_id=analyzer_id)
            for run in range(runs):
                row = observe(producer, path)
                row |= {"document": path.name, "analyzer": analyzer_id, "run": run}
                rows.append(row)
                print(
                    f"  {path.name[:34]:34} {analyzer_id[-6:]:6} run{run} "
                    f"segs={row['segments']:>2} sub-page={row['sub_page_segments']:>2} "
                    f"rescued={row['rescued_pages']} "
                    f"unexplained={row['unexplained_chars']:>4} {row['signature'][:44]}"
                )

    print("\n" + "=" * 78)
    for path in pdfs:
        print(f"\n{path.name}")
        for analyzer_id in (PAGE_ANALYZER, IN_PAGE_ANALYZER):
            these = [r for r in rows if r["document"] == path.name and r["analyzer"] == analyzer_id]
            if not these:
                continue
            signatures = Counter(r["signature"] for r in these)
            label = "page-level " if analyzer_id == PAGE_ANALYZER else "in-page   "
            print(
                f"  {label} segments={[r['segments'] for r in these]} "
                f"sub-page={[r['sub_page_segments'] for r in these]} "
                f"rescued={[r['rescued_pages'] for r in these]} "
                f"coverage_ok={all(r['unexplained_chars'] == 0 for r in these)}"
            )
            if len(signatures) > 1:
                print(f"    NON-DETERMINISTIC across {len(these)} runs: {dict(signatures)}")

    out = Path("out/eval/inpage_bench.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
