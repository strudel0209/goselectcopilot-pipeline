"""Does tiling help or hurt when the source crop is already downsampled?

Tiling only buys resolution when the source is *above* the model's cap. DI's
server-side crops come back around 1480x990, barely above Azure's 768 px short
edge, so tiling may fragment the diagram for no resolution gain.
"""

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, "/workspaces/MODP/src")
from dotenv import load_dotenv

load_dotenv("/workspaces/MODP/.env")

from goselect_docproc.contracts import ContentType, Span, WorkItem  # noqa: E402
from goselect_docproc.extractors import ModelExtractor, SegmentContext  # noqa: E402
from goselect_docproc.models import AzureOpenAIModel  # noqa: E402
from goselect_docproc.tiling import VisionLimits, plan_tiles  # noqa: E402

import os  # noqa: E402

CROP = Path("/workspaces/MODP/.cache/93fa542cd1aa2c9a-figure-1.1.png")
TRUTH = {"VFD-H1", "VFD-H2", "VFD-H3", "VFD-J4"}
SSS = {"SSS-W5", "SSS-W6"}

blob = CROP.read_bytes()
width, height = struct.unpack(">II", blob[16:24])

ITEM = WorkItem(
    job_id="probe", correlation_id="probe", file_id="f1", file_ordinal=0,
    segment_id="f1-seg-001", content_type=ContentType.DRAWING,
    first_page=1, last_page=1, spans=[Span(offset=0, length=0)],
    layout_uri="x", figures=["1.1"],
)

CASES = {
    "whole image (no tiling)": VisionLimits("whole", 10**6, 10**9),
    "azure 768 tiles": VisionLimits.azure_openai(),
}

for deployment in sys.argv[1:]:
    model = AzureOpenAIModel(
        endpoint=os.environ["CONTENTUNDERSTANDING_ENDPOINT"], deployment=deployment
    )
    for label, limits in CASES.items():
        tiles = plan_tiles(width, height, limits)
        extractor = ModelExtractor(
            content_type=ContentType.DRAWING, model=model, vision_limits=limits, max_tiles=40
        )
        context = SegmentContext(content="", item=ITEM, figures={"1.1": blob})
        try:
            payload = extractor.extract(context)
        except Exception as exc:  # noqa: BLE001
            print(f"{deployment:14s} {label:24s} FAILED {type(exc).__name__}: {str(exc)[:120]}")
            continue

        tags = {(v.tag or "").upper() for v in payload.vfds if v.tag}
        tags |= {(m.tag or "").upper() for m in payload.motors if m.tag}
        print(
            f"{deployment:14s} {label:24s} {len(tiles)} img  "
            f"found={len(tags):2d}  VFD {len(TRUTH & tags)}/4  "
            f"SSS-as-drive={sorted(SSS & tags) or '-'}"
        )
        print(f"{'':14s} {'':24s} tags={sorted(tags)[:10]}")
