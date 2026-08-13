"""Produce one self-contained HTML report for a document package.

The point of this file is that nobody should have to read Python, a JSON blob or
a chat transcript to see what the pipeline did. Run it, open the file, show it.

    python eval/report.py sample_docs/98878_1_HoweyVFDs.pdf sample_docs/98878_2_HoweyOneline.pdf
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parent.parent / ".env"))

from goselect_docproc.cli import _model, _producer, _sources  # noqa: E402
from goselect_docproc.contracts import ContentType  # noqa: E402
from goselect_docproc.extractors import ModelExtractor, default_extractors  # noqa: E402
from goselect_docproc.pipeline import Pipeline, PipelineConfig  # noqa: E402
from goselect_docproc.tiling import VisionLimits  # noqa: E402

CSS = """
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1b1f24}
.wrap{max-width:1080px;margin:0 auto;padding:32px}
h1{font-size:24px;margin:0 0 4px} h2{font-size:17px;margin:34px 0 10px;padding-bottom:6px;border-bottom:2px solid #d8dce1}
.sub{color:#57606a;margin:0 0 8px}
table{border-collapse:collapse;width:100%;background:#fff;margin:10px 0;font-size:13px}
th,td{border:1px solid #d8dce1;padding:7px 9px;text-align:left;vertical-align:top}
th{background:#eef1f4;font-weight:600}
.card{background:#fff;border:1px solid #d8dce1;padding:14px 16px;margin:10px 0}
.ok{color:#1a7f37;font-weight:600} .bad{color:#cf222e;font-weight:600} .warn{color:#9a6700;font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.TEXT{background:#ddf4ff;color:#0969da} .DRAWING{background:#fff1e5;color:#bc4c00}
.SCHEDULE{background:#dafbe1;color:#1a7f37} .OTHER{background:#eee;color:#57606a}
code{background:#f0f2f4;padding:1px 5px;font-size:12px}
.q{color:#57606a;font-style:italic}
.big{font-size:26px;font-weight:700}
.grid{display:flex;gap:14px;flex-wrap:wrap} .grid .card{flex:1;min-width:150px;text-align:center}
</style>"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def pill(kind: str) -> str:
    return f'<span class="pill {kind}">{kind}</span>'


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    producer_name = next(
        (f.split("=", 1)[1] for f in flags if f.startswith("--producer=")), "di-layout"
    )
    pdfs = args or ["sample_docs/98878_1_HoweyVFDs.pdf", "sample_docs/98878_2_HoweyOneline.pdf"]
    text_model = os.getenv("REPORT_MODEL", "gpt-5.4-mini")
    drawing_model = os.getenv("REPORT_DRAWING_MODEL", "gpt-5.6-sol")

    limits = VisionLimits.azure_openai()
    extractors = default_extractors(_model(text_model), limits)
    extractors[ContentType.DRAWING] = ModelExtractor(
        content_type=ContentType.DRAWING, model=_model(drawing_model), vision_limits=limits
    )

    started = time.perf_counter()
    pipeline = Pipeline(
        producer=_producer(producer_name, Path(".cache")),
        extractors=extractors,
        config=PipelineConfig(cache_dir=Path(".cache"), output_dir=Path("out")),
    )
    manifest, job, results = pipeline.run(_sources(pdfs))
    elapsed = time.perf_counter() - started

    out: list[str] = [
        f"<!doctype html><meta charset=utf-8><title>GoSelect Copilot — package report</title><style>{CSS}",
        '<div class="wrap">',
        "<h1>GoSelect Copilot — document package report</h1>",
        f'<p class="sub">{esc(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))} · '
        f"engine <code>{esc(producer_name)}</code> · text model <code>{esc(text_model)}</code> · "
        f"drawing model <code>{esc(drawing_model)}</code> · {elapsed:.0f}s end to end</p>",
    ]

    # ---- 1. input -----------------------------------------------------------
    out.append("<h2>1. What was submitted</h2><table><tr><th>File</th><th>Pages</th>"
               "<th>Characters</th><th>SHA-256</th></tr>")
    for f in manifest.files:
        name = f.source_uri.rsplit("/", 1)[-1]
        out.append(f"<tr><td>{esc(name)}</td><td>{f.page_count}</td>"
                   f"<td>{f.content_chars:,}</td><td><code>{esc(f.content_sha256[:16])}</code></td></tr>")
    out.append("</table>")

    # ---- 2. segmentation ----------------------------------------------------
    out.append("<h2>2. How it was split — and why that matters</h2>"
               "<p class='sub'>Today ABB labels a whole <em>file</em> once. This labels runs of pages, "
               "then splits inside a page. Each row below becomes one queue message.</p>"
               "<table><tr><th>Segment</th><th>File</th><th>Pages</th><th>Type</th><th>Confidence</th>"
               "<th>Route</th><th>Section it belongs to</th></tr>")
    for s in sorted(manifest.segments, key=manifest.sort_key):
        route = s.route(0.25).value
        cls = "ok" if route == "AUTO" else "warn"
        out.append(
            f"<tr><td><code>{esc(s.segment_id)}</code></td><td>{esc(s.file_id)}</td>"
            f"<td>{s.first_page}–{s.last_page}</td><td>{pill(s.content_type.value)}</td>"
            f"<td>{s.confidence:.3f}</td><td class='{cls}'>{route}</td>"
            f"<td>{esc(s.section_root)}</td></tr>"
        )
    out.append("</table>")

    # ---- 3. coverage --------------------------------------------------------
    out.append("<h2>3. Proof that nothing was dropped</h2>"
               "<p class='sub'>Every character is either claimed by a segment or declared page furniture. "
               "If a paragraph went missing, nobody would notice — so this is asserted, not assumed.</p>"
               "<table><tr><th>File</th><th>Claimed</th><th>Furniture</th><th>Unexplained</th>"
               "<th>Accounted</th></tr>")
    for file_id, c in manifest.coverage.items():
        state = "ok" if c.ok else "bad"
        out.append(f"<tr><td>{esc(file_id)}</td><td>{c.covered_chars:,}</td>"
                   f"<td>{c.furniture_chars:,}</td><td>{c.unexplained_chars:,}</td>"
                   f"<td class='{state}'>{c.accounted_ratio:.2%}</td></tr>")
    out.append("</table>")

    # ---- 4. section index ---------------------------------------------------
    index = next(iter(pipeline.analyses().values())).section_index
    out.append(f"<h2>4. Where each value can be traced back to</h2>"
               f"<p class='sub'>Strategy <code>{esc(index.strategy)}</code>. "
               f"Any extracted value resolves to the last heading before it, by character offset — "
               f"not by 'nearest text above', which is wrong on landscape pages.</p><div class='card'>")
    for n in index.nodes[:24]:
        out.append(f"{'&nbsp;' * 4 * (n.level - 1)}<code>p{n.page}</code> {esc(n.heading)}<br>")
    out.append("</div>")

    # ---- 5. extraction ------------------------------------------------------
    out.append("<h2>5. What each segment produced</h2><table><tr><th>Segment</th><th>Type</th>"
               "<th>Status</th><th>Model</th><th>Latency</th><th>VFDs</th><th>Motors</th>"
               "<th>Pairs</th><th>Issues</th></tr>")
    for r in sorted(results, key=lambda r: (r.file_ordinal, r.start_offset)):
        p = r.payload
        cls = {"DONE": "ok", "REVIEW": "warn", "FAILED": "bad"}.get(r.status.value, "")
        out.append(
            f"<tr><td><code>{esc(r.segment_id)}</code></td><td>{pill(r.content_type.value)}</td>"
            f"<td class='{cls}'>{r.status.value}</td><td>{esc(r.model)}</td>"
            f"<td>{(r.latency_ms or 0) / 1000:.1f}s</td>"
            f"<td>{len(p.vfds) if p else 0}</td><td>{len(p.motors) if p else 0}</td>"
            f"<td>{len(p.pairs) if p else 0}</td><td>{esc('; '.join(r.errors)[:80] or '—')}</td></tr>"
        )
    out.append("</table>")

    # ---- 6. the result ------------------------------------------------------
    pl = job.payload
    out.append("<h2>6. The structured result handed to GoSelect</h2>")
    out.append('<div class="grid">'
               f'<div class="card"><div class="big">{len(pl.vfds)}</div>VFD records</div>'
               f'<div class="card"><div class="big">{len(pl.motors)}</div>Motor records</div>'
               f'<div class="card"><div class="big">{len(pl.pairs)}</div>VFD–motor pairs</div>'
               f'<div class="card"><div class="big">{len(job.conflicts)}</div>Conflicts</div>'
               f'<div class="card"><div class="big">{len(job.review_required)}</div>Need review</div>'
               "</div>")

    if pl.vfds or pl.motors:
        out.append("<table><tr><th>Kind</th><th>Tag</th><th>Values</th><th>Traced to</th>"
                   "<th>Verbatim from the document</th></tr>")
        for kind, items in (("VFD", pl.vfds), ("MOTOR", pl.motors)):
            for spec in items:
                bits = []
                for name in ("power", "voltage", "current", "frequency", "speed"):
                    q = getattr(spec, name, None)
                    if q is not None and (q.value is not None or q.raw):
                        bits.append(f"{name}={esc(q.raw or q.value)}")
                ev = spec.evidence[0] if spec.evidence else None
                out.append(
                    f"<tr><td>{kind}</td><td><code>{esc(spec.tag) if spec.tag else '<em>project-wide</em>'}</code></td>"
                    f"<td>{'<br>'.join(bits) or '—'}</td>"
                    f"<td>{esc(ev.section_path) if ev else '—'}<br><small>page {ev.page if ev else '?'}</small></td>"
                    f"<td class='q'>{esc((ev.verbatim or '')[:150]) if ev else ''}</td></tr>"
                )
        out.append("</table>")

    if pl.pairs:
        out.append("<table><tr><th>Pair</th><th>VFD</th><th>Motor</th><th>Asserted by</th>"
                   "<th>Confidence</th></tr>")
        for pair in pl.pairs:
            out.append(f"<tr><td><code>{esc(pair.pair_id)}</code></td><td>{esc(pair.vfd_tag)}</td>"
                       f"<td>{esc(pair.motor_tag)}</td><td>{pill(pair.origin.value)}</td>"
                       f"<td>{pair.confidence:.2f}</td></tr>")
        out.append("</table>")

    # ---- 7. what it refused to guess ---------------------------------------
    out.append("<h2>7. What it refused to guess</h2>"
               "<p class='sub'>Disagreements are surfaced, never silently resolved. "
               "This is the difference between a quotation input and a plausible-looking one.</p>")
    if job.conflicts:
        out.append("<table><tr><th>Field</th><th>Competing values</th><th>Asserted by</th></tr>")
        for c in job.conflicts:
            out.append(f"<tr><td><code>{esc(c.field)}</code></td><td>{esc(' vs '.join(c.values))}</td>"
                       f"<td>{' '.join(pill(o.value) for o in c.origins)}</td></tr>")
        out.append("</table>")
    else:
        out.append("<div class='card'>No conflicts in this package.</div>")

    if pl.notes:
        out.append("<div class='card'><strong>Model notes</strong><ul>")
        for note in pl.notes[:10]:
            out.append(f"<li class='q'>{esc(note)}</li>")
        out.append("</ul></div>")

    # ---- 8. scorecard -------------------------------------------------------
    card = Path("out/eval/scorecard.json")
    if card.exists():
        data = json.loads(card.read_text())
        out.append("<h2>8. Measured against a hand-labelled answer key</h2>"
                   "<table><tr><th>Gate</th><th>Measured</th><th>Required</th><th>Result</th></tr>")
        for gate, required in data["gates"].items():
            value = data["measured"].get(gate)
            if value is None:
                verdict, shown, cls = "NOT MEASURED", "n/a", "warn"
            else:
                ok = value >= required
                verdict, shown, cls = ("PASS" if ok else "FAIL"), f"{value:.3f}", ("ok" if ok else "bad")
            out.append(f"<tr><td>{esc(gate)}</td><td>{shown}</td><td>{required:.2f}</td>"
                       f"<td class='{cls}'>{verdict}</td></tr>")
        out.append("</table>")

    out.append(f"<h2>Job outcome</h2><div class='card'>Status <strong>{job.status.value}</strong> — "
               f"{job.segments_done}/{job.segments_expected} segments complete, "
               f"{job.segments_failed} failed.</div>")
    out.append("</div>")

    # out/runs/<package>/<engine>/ - one folder per package per engine, so two
    # engines on the same documents sit side by side and diff cleanly.
    package = os.path.commonprefix([Path(p).stem for p in pdfs]).rstrip("_-") or Path(pdfs[0]).stem
    run_dir = Path("out/runs") / package / producer_name
    run_dir.mkdir(parents=True, exist_ok=True)

    target = run_dir / "report.html"
    target.write_text("\n".join(out), encoding="utf-8")

    summary = {
        "package": package,
        "engine": producer_name,
        "documents": [Path(p).name for p in pdfs],
        "text_model": text_model,
        "drawing_model": drawing_model,
        "seconds": round(elapsed, 1),
        "segments": [
            {"id": s.segment_id, "pages": [s.first_page, s.last_page],
             "type": s.content_type.value, "confidence": s.confidence}
            for s in sorted(manifest.segments, key=manifest.sort_key)
        ],
        "coverage": {k: c.accounted_ratio for k, c in manifest.coverage.items()},
        "headings": len(index.nodes),
        "section_strategy": index.strategy,
        "vfds": len(pl.vfds), "motors": len(pl.motors), "pairs": len(pl.pairs),
        "conflicts": len(job.conflicts), "review": len(job.review_required),
        "status": job.status.value,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (run_dir / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json"), indent=2, default=str))
    (run_dir / "results.json").write_text(json.dumps([r.model_dump(mode="json") for r in results], indent=2, default=str))
    (run_dir / "job.json").write_text(json.dumps(job.model_dump(mode="json"), indent=2, default=str))
    print(f"wrote {run_dir}/ (report.html, summary.json, manifest.json, results.json, job.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
