"""Command line entry point.

    goselect-docproc segment  <pdf>...   # manifest + markdown coverage
    goselect-docproc plan     <pdf>...   # the queue messages that would be sent
    goselect-docproc run      <pdf>...   # full pipeline
    goselect-docproc tiles    <w> <h>    # vision legibility budget for a drawing

``segment`` and ``plan`` submit one paid CU analysis per file on every run.
``run`` also makes separate model requests for drawings when configured.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

from azure.core.exceptions import ResourceNotFoundError

from .contracts import ContentType
from .extractors import ModelExtractor, NullModel, default_extractors
from .pipeline import Pipeline, PipelineConfig
from .producers import available as available_producers
from .producers.content_understanding import DEFAULT_ANALYZER_ID, DEFAULT_COMPLETION_MODEL
from .tiling import VisionLimits, assess, plan_tiles


def _producer(name: str, analyzer_id: str | None = None):
    """Swap the front half by configuration. The spine never changes."""
    from .producers import ContentUnderstandingProducer

    if name == "content-understanding":
        from .producers.content_understanding import analyzer_configuration

        client = _content_understanding_client()
        chosen_id = analyzer_id or os.getenv("CU_ANALYZER_ID", DEFAULT_ANALYZER_ID)
        analyzer_configuration(client, chosen_id)
        return ContentUnderstandingProducer(
            client, analyzer_id=chosen_id,
        )

    raise SystemExit(f"unknown producer {name!r}; available: {available_producers()}")


def _content_understanding_client():
    from azure.ai.contentunderstanding import ContentUnderstandingClient

    endpoint = os.getenv("CONTENTUNDERSTANDING_ENDPOINT")
    if not endpoint:
        raise SystemExit("CONTENTUNDERSTANDING_ENDPOINT is not set")

    key = os.getenv("CONTENTUNDERSTANDING_API_KEY")
    if key:
        from azure.core.credentials import AzureKeyCredential

        credential = AzureKeyCredential(key)
    else:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
    return ContentUnderstandingClient(
        endpoint=endpoint, credential=credential, api_version="2026-06-01-preview",
    )


def _sources(paths: list[str]) -> dict[str, tuple[bytes, str]]:
    return {
        f"f{i + 1}": (Path(p).read_bytes(), f"file://{Path(p).resolve()}")
        for i, p in enumerate(paths)
    }


def _run_dir(args: argparse.Namespace) -> Path:
    """A unique run directory unless --out explicitly chooses a reusable path."""
    if args.out:
        return Path(args.out)
    stems = [Path(p).stem for p in args.pdf]
    package = os.path.commonprefix(stems).rstrip("_-") or stems[0]
    return Path("out/runs") / package / str(uuid4())


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
    out = _run_dir(args)
    pipeline = Pipeline(
        producer=_producer(args.producer, args.analyzer_id),
        extractors={},
        config=PipelineConfig(output_dir=out),
    )
    manifest = pipeline.segment(_sources(args.pdf))
    _report_segments(manifest, args.review_threshold)
    failures = _report_coverage(manifest)
    path = _write(out, "manifest.json", manifest.model_dump(mode="json"))
    print(f"\nwrote {path}")
    return 1 if failures and args.strict else 0


def cmd_plan(args: argparse.Namespace) -> int:
    out = _run_dir(args)
    pipeline = Pipeline(
        producer=_producer(args.producer, args.analyzer_id),
        extractors={},
        config=PipelineConfig(output_dir=out),
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
    path = _write(out, "work_items.json", [i.model_dump(mode="json") for i in items])
    print(f"\nwrote {path}")
    return 0


def _model(name: str | None):
    """``None`` disables separate model requests, not CU analysis charges."""
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
    out = _run_dir(args)
    model = _model(args.drawing_model)
    limits = VisionLimits.azure_openai()
    extractors = default_extractors(model, limits)
    pipeline = Pipeline(
        producer=_producer(args.producer, args.analyzer_id),
        extractors=extractors,
        config=PipelineConfig(
            output_dir=out,
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

    _write(out, "manifest.json", manifest.model_dump(mode="json"))
    _write(out, "results.json", [r.model_dump(mode="json") for r in results])
    _write(out, "job.json", job.model_dump(mode="json"))

    from .deliver import contract_payload, review_sheet

    _write(out, "deliverable.json", contract_payload(job))
    document = ", ".join(Path(p).name for p in args.pdf)
    (out / "review.md").write_text(review_sheet(job, document=document))

    if args.markdown:
        from .assemble import reassemble_markdown

        _write(out, "reassembled.md", reassemble_markdown(manifest, pipeline.content_by_file()))
    print(
        f"\nwrote {out}/\n"
        f"  deliverable.json  the agreed GoSelect schema\n"
        f"  review.md         the same values, for a human to check\n"
        f"  manifest.json     segments, sections and markdown coverage\n"
        f"  results.json      one record per segment\n"
        f"  job.json          status, conflicts, what needs review"
    )
    return 0 if job.status.value == "DONE" else 1


def cmd_setup_analyzer(args: argparse.Namespace) -> int:
    """Explicitly replace one shared field analyzer and its router."""
    from .field_schema import flat_fields, load_field_schema
    from .producers.content_understanding import (
        ensure_analyzer,
        field_analyzer,
        router_analyzer,
    )

    client = _content_understanding_client()
    out = Path(args.out) / "analyzers"
    base = args.analyzer_id
    completion_model = args.completion_model
    deployment = (client.get_defaults().model_deployments or {}).get(completion_model)
    if not deployment or (os.getenv("REPORT_MODEL") and deployment != os.environ["REPORT_MODEL"]):
        raise SystemExit(
            f"CU model {completion_model!r} needs a resource mapping to the intended deployment; "
            "use --completion-model for its logical model identifier"
        )

    analyzer_id = f"{base}Fields"
    analyzer = field_analyzer(load_field_schema(), completion_model)
    ensure_analyzer(client, analyzer_id, analyzer)
    routes = {category: analyzer_id for category in ("text", "schedule")}
    print(f"analyzer {analyzer_id} ready ({len(flat_fields())} customer fields plus extraction metadata)")
    _write(out, f"{analyzer_id}.json", analyzer.as_dict())

    router = router_analyzer(
        completion_model, in_page_segments=args.in_page_segments, field_analyzer_ids=routes,
    )
    ensure_analyzer(client, base, router)
    print(f"analyzer {base} ready, routing {routes}")
    path = _write(out, f"{base}.json", router.as_dict())
    print(f"definitions written to {path.parent}/")
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
    # Before the parser: argparse evaluates env-derived defaults at add_argument
    # time, so loading later silently ignores CU_ANALYZER_ID.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(prog="goselect-docproc")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("pdf", nargs="+")
        p.add_argument(
            "--out",
            default=None,
            help="where results go; defaults to out/runs/<package>/<run-id>",
        )
        p.add_argument("--review-threshold", type=float, default=0.25)
        p.add_argument(
            "--producer",
            default="content-understanding",
            help=f"segment producer; one of {available_producers()}",
        )
        p.add_argument(
            "--analyzer-id",
            default=None,
            help="Content Understanding router; defaults to $CU_ANALYZER_ID "
            f"then {DEFAULT_ANALYZER_ID}",
        )

    p_segment = sub.add_parser("segment", help="segment only; paid CU analysis")
    common(p_segment)
    p_segment.add_argument("--strict", action="store_true", help="exit 1 on unexplained content loss")
    p_segment.set_defaults(func=cmd_segment)

    p_plan = sub.add_parser("plan", help="show the queue messages that would be sent")
    common(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="full pipeline")
    common(p_run)
    p_run.add_argument("--workers", type=int, default=4)
    p_run.add_argument("--drawing-model", default=os.getenv("REPORT_DRAWING_MODEL"), help="drawing deployment; defaults to REPORT_DRAWING_MODEL; missing model fails drawing segments")
    p_run.add_argument("--markdown", action="store_true", help="also emit reassembled.md")
    p_run.set_defaults(func=cmd_run)

    p_tiles = sub.add_parser("tiles", help="vision legibility budget for a drawing")
    p_tiles.add_argument("width", type=int)
    p_tiles.add_argument("height", type=int)
    p_tiles.add_argument("--dpi", type=int, default=300)
    p_tiles.add_argument("--point-size", type=float, default=10.0)
    p_tiles.set_defaults(func=cmd_tiles)

    p_setup = sub.add_parser(
        "setup-analyzer", help="one-off: create the Content Understanding router analyzer"
    )
    p_setup.add_argument("--analyzer-id", default=os.getenv("CU_ANALYZER_ID", DEFAULT_ANALYZER_ID))
    p_setup.add_argument(
        "--completion-model", default=os.getenv("REPORT_MODEL", DEFAULT_COMPLETION_MODEL),
        help="logical CU model identifier mapped to a deployment in resource defaults",
    )
    p_setup.set_defaults(in_page_segments=True)
    p_setup.add_argument("--out", default="out")
    p_setup.set_defaults(func=cmd_setup_analyzer)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except ResourceNotFoundError as exc:
        if "ModelNotFound" not in str(exc):
            raise
        wanted = getattr(args, "analyzer_id", None) or os.getenv(
            "CU_ANALYZER_ID", DEFAULT_ANALYZER_ID
        )
        print(
            f"analyzer {wanted!r} does not exist on this resource.\n"
            f"Create it, or point at one that exists:\n"
            f"  goselect-docproc setup-analyzer --analyzer-id {wanted}\n"
            f"  ...or set CU_ANALYZER_ID in .env, or pass --analyzer-id",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
