"""Command line entry point.

    goselect-docproc segment  <pdf>...   # manifest + coverage, no model spend
    goselect-docproc plan     <pdf>...   # the queue messages that would be sent
    goselect-docproc run      <pdf>...   # full pipeline
    goselect-docproc tiles    <w> <h>    # vision legibility budget for a drawing

``segment`` and ``plan`` cost one Layout call per file and nothing else, so they
are safe to run repeatedly while tuning.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .contracts import ContentType
from .extractors import ModelExtractor, NullModel, default_extractors
from .layout import LayoutClient
from .pipeline import Pipeline, PipelineConfig
from .producers import available as available_producers
from .producers.content_understanding import DEFAULT_ANALYZER_ID
from .tiling import VisionLimits, assess, plan_tiles


def _layout_client(cache_dir: Path) -> LayoutClient:
    endpoint = os.getenv("DOCUMENTINTELLIGENCE_ENDPOINT")
    if not endpoint:
        # Fully cached corpora replay with no credentials, which is what makes the
        # eval set runnable in CI.
        logging.getLogger(__name__).warning(
            "DOCUMENTINTELLIGENCE_ENDPOINT unset; cache-only mode"
        )
        return LayoutClient(None, cache_dir)

    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential
    from azure.identity import DefaultAzureCredential

    key = os.getenv("DOCUMENTINTELLIGENCE_API_KEY")
    credential = AzureKeyCredential(key) if key else DefaultAzureCredential()
    return LayoutClient(
        DocumentIntelligenceClient(endpoint=endpoint, credential=credential), cache_dir
    )


def _producer(name: str, cache_dir: Path):
    """Swap the front half by configuration. The spine never changes."""
    from .producers import ContentUnderstandingProducer, DILayoutProducer

    if name == "di-layout":
        return DILayoutProducer(_layout_client(cache_dir))

    if name == "content-understanding":
        return ContentUnderstandingProducer(
            _content_understanding_client(), analyzer_id=os.getenv("CU_ANALYZER_ID", DEFAULT_ANALYZER_ID)
        )

    raise SystemExit(f"unknown producer {name!r}; available: {available_producers()}")


def _content_understanding_client():
    from .producers.content_understanding import AzureContentUnderstandingClient

    endpoint = os.getenv("CONTENTUNDERSTANDING_ENDPOINT")
    if not endpoint:
        raise SystemExit("CONTENTUNDERSTANDING_ENDPOINT is not set")
    return AzureContentUnderstandingClient(
        endpoint, api_key=os.getenv("CONTENTUNDERSTANDING_API_KEY") or None
    )


def _sources(paths: list[str]) -> dict[str, tuple[bytes, str]]:
    return {
        f"f{i + 1}": (Path(p).read_bytes(), f"file://{Path(p).resolve()}")
        for i, p in enumerate(paths)
    }


def _write(output_dir: Path, name: str, payload: object) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / name
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    path.write_text(text, encoding="utf-8")
    return path


def _report_coverage(manifest) -> int:
    failures = 0
    print("\ncoverage")
    for file_id, coverage in manifest.coverage.items():
        flag = "ok" if coverage.ok else "UNEXPLAINED LOSS"
        failures += 0 if coverage.ok else 1
        print(
            f"  {file_id}: accounted {coverage.accounted_ratio:.2%} "
            f"(claimed {coverage.covered_chars}, furniture {coverage.furniture_chars}, "
            f"unexplained {coverage.unexplained_chars}) [{flag}]"
        )
        for sample in coverage.unexplained_samples[:3]:
            print(f"      lost: {sample!r}")
    return failures


def _report_segments(manifest, threshold: float) -> None:
    print(f"\nsegments ({len(manifest.segments)}) - producer {manifest.producer}")
    for segment in sorted(manifest.segments, key=manifest.sort_key):
        kinds = sorted({r.kind.value for r in segment.regions})
        print(
            f"  {segment.segment_id}  p{segment.first_page}-{segment.last_page}  "
            f"{segment.content_type.value:9} conf={segment.confidence:<6} "
            f"{segment.route(threshold).value:6} regions={kinds} "
            f"section={segment.section_root!r}"
        )


def cmd_segment(args: argparse.Namespace) -> int:
    pipeline = Pipeline(
        producer=_producer(args.producer, Path(args.cache_dir)),
        extractors={},
        config=PipelineConfig(cache_dir=Path(args.cache_dir), output_dir=Path(args.out)),
    )
    manifest = pipeline.segment(_sources(args.pdf))
    _report_segments(manifest, args.review_threshold)
    failures = _report_coverage(manifest)
    path = _write(Path(args.out), "manifest.json", manifest.model_dump(mode="json"))
    print(f"\nwrote {path}")
    return 1 if failures and args.strict else 0


def cmd_plan(args: argparse.Namespace) -> int:
    pipeline = Pipeline(
        producer=_producer(args.producer, Path(args.cache_dir)),
        extractors={},
        config=PipelineConfig(cache_dir=Path(args.cache_dir), output_dir=Path(args.out)),
    )
    manifest = pipeline.segment(_sources(args.pdf))
    items = pipeline.plan(manifest)
    print(f"\n{len(items)} queue messages")
    for item in items:
        print(
            f"  {item.segment_id}  {item.content_type.value:9} "
            f"p{item.first_page}-{item.last_page} spans={len(item.spans)} "
            f"figures={len(item.figures)} highRes={item.high_resolution} formulas={item.formulas}"
        )
    path = _write(Path(args.out), "work_items.json", [i.model_dump(mode="json") for i in items])
    print(f"\nwrote {path}")
    return 0


def _model(name: str | None):
    """``None`` keeps the pipeline runnable end to end with no spend."""
    if not name:
        return NullModel()

    from .models import AzureOpenAIModel

    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("CONTENTUNDERSTANDING_ENDPOINT")
    if not endpoint:
        raise SystemExit("AZURE_OPENAI_ENDPOINT is not set")
    return AzureOpenAIModel(
        endpoint=endpoint,
        deployment=name,
        api_key=os.getenv("AZURE_OPENAI_API_KEY") or None,
    )


def cmd_run(args: argparse.Namespace) -> int:
    model = _model(args.model)
    if args.standard_tier:
        limits = VisionLimits.standard()
    elif args.model or args.drawing_model:
        limits = VisionLimits.azure_openai()
    else:
        limits = VisionLimits.high_resolution()
    extractors = default_extractors(model, limits)
    if args.drawing_model:
        extractors[ContentType.DRAWING] = ModelExtractor(
            content_type=ContentType.DRAWING,
            model=_model(args.drawing_model),
            vision_limits=limits,
        )
    pipeline = Pipeline(
        producer=_producer(args.producer, Path(args.cache_dir)),
        extractors=extractors,
        config=PipelineConfig(
            cache_dir=Path(args.cache_dir),
            output_dir=Path(args.out),
            max_workers=args.workers,
            review_threshold=args.review_threshold,
        ),
    )
    manifest, job, results = pipeline.run(_sources(args.pdf))

    _report_segments(manifest, args.review_threshold)
    _report_coverage(manifest)
    print(
        f"\njob {job.status.value}: {job.segments_done}/{job.segments_expected} done, "
        f"{job.segments_failed} failed, {len(job.conflicts)} conflicts, "
        f"{len(job.review_required)} needing review"
    )

    out = Path(args.out)
    _write(out, "manifest.json", manifest.model_dump(mode="json"))
    _write(out, "results.json", [r.model_dump(mode="json") for r in results])
    _write(out, "job.json", job.model_dump(mode="json"))
    if args.markdown:
        from .assemble import reassemble_markdown

        _write(out, "reassembled.md", reassemble_markdown(manifest, pipeline.content_by_file()))
    print(f"wrote {out}/manifest.json, results.json, job.json")
    return 0 if job.status.value != "FAILED" else 1


def cmd_bench(args: argparse.Namespace) -> int:
    from . import bench as bench_module

    producers = {}
    for name in args.producers.split(","):
        name = name.strip()
        if not name:
            continue
        try:
            producers[name] = _producer(name, Path(args.cache_dir))
        except SystemExit as exc:
            print(f"skipping {name}: {exc}", file=sys.stderr)
        except (ImportError, KeyError) as exc:
            print(f"skipping {name}: not configured ({exc})", file=sys.stderr)

    if not producers:
        print("no producers configured", file=sys.stderr)
        return 2

    documents = {Path(p).name: Path(p).read_bytes() for p in args.pdf}
    rows = bench_module.run(producers, documents)
    print(bench_module.render(rows))
    path = bench_module.write(rows, Path(args.out))
    print(f"\nwrote {path}")
    return 0 if all(r.ok for r in rows) else 1


def cmd_setup_analyzer(args: argparse.Namespace) -> int:
    """One-off: PUT the Content Understanding router analyzer."""
    from .producers.content_understanding import ROUTER_ANALYZER

    client = _content_understanding_client()
    client.ensure_analyzer(args.analyzer_id, ROUTER_ANALYZER)
    print(f"analyzer {args.analyzer_id} ready at {client.endpoint}")
    _write(Path(args.out), "cu-router.json", ROUTER_ANALYZER)
    return 0


def cmd_tiles(args: argparse.Namespace) -> int:
    for limits in (VisionLimits.standard(), VisionLimits.high_resolution()):
        whole = assess(args.width, args.height, limits, args.dpi, args.point_size)
        boxes = plan_tiles(args.width, args.height, limits)
        first = boxes[0]
        tile = assess(first[2] - first[0], first[3] - first[1], limits, args.dpi, args.point_size)
        print(f"\n{limits.name} (long edge {limits.max_long_edge}px, {limits.max_visual_tokens} tokens)")
        print(f"  whole sheet : {whole.summary()}")
        print(f"  tiled       : {len(boxes)} tiles, each {tile.summary()}")
        print(f"  token cost  : whole={whole.tokens}  tiled={len(boxes) * tile.tokens}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="goselect-docproc")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("pdf", nargs="+")
        p.add_argument("--cache-dir", default=".cache")
        p.add_argument("--out", default="out")
        p.add_argument("--review-threshold", type=float, default=0.25)
        p.add_argument(
            "--producer",
            default="di-layout",
            help=f"segment producer; one of {available_producers()}",
        )

    p_segment = sub.add_parser("segment", help="segment only; no model spend")
    common(p_segment)
    p_segment.add_argument("--strict", action="store_true", help="exit 1 on unexplained content loss")
    p_segment.set_defaults(func=cmd_segment)

    p_plan = sub.add_parser("plan", help="show the queue messages that would be sent")
    common(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="full pipeline")
    common(p_run)
    p_run.add_argument("--workers", type=int, default=4)
    p_run.add_argument("--model", default=None, help="Foundry deployment name; omit for zero-spend NullModel")
    p_run.add_argument("--drawing-model", default=None, help="override the deployment used for DRAWING segments")
    p_run.add_argument("--standard-tier", action="store_true", help="model without high-res vision")
    p_run.add_argument("--markdown", action="store_true", help="also emit reassembled.md")
    p_run.set_defaults(func=cmd_run)

    p_tiles = sub.add_parser("tiles", help="vision legibility budget for a drawing")
    p_tiles.add_argument("width", type=int)
    p_tiles.add_argument("height", type=int)
    p_tiles.add_argument("--dpi", type=int, default=300)
    p_tiles.add_argument("--point-size", type=float, default=10.0)
    p_tiles.set_defaults(func=cmd_tiles)

    p_bench = sub.add_parser("bench", help="compare producers on the same corpus")
    p_bench.add_argument("pdf", nargs="+")
    p_bench.add_argument("--cache-dir", default=".cache")
    p_bench.add_argument("--out", default="out")
    p_bench.add_argument(
        "--producers",
        default="di-layout",
        help=f"comma-separated; available: {','.join(available_producers())}",
    )
    p_bench.set_defaults(func=cmd_bench)

    p_setup = sub.add_parser(
        "setup-analyzer", help="one-off: create the Content Understanding router analyzer"
    )
    p_setup.add_argument("--analyzer-id", default=os.getenv("CU_ANALYZER_ID", DEFAULT_ANALYZER_ID))
    p_setup.add_argument("--out", default="out")
    p_setup.set_defaults(func=cmd_setup_analyzer)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
