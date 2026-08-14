"""Find the cheapest resolution that reads a known-hard tag pair.

Plainville sheet 1 contains VFD-401 feeding RWP-401. Content Understanding read
them as VD-401 and RWP-A01. This walks the DPI ladder and reports what each
setting costs and whether the tags survive.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, "/workspaces/MODP/src")
from dotenv import load_dotenv

load_dotenv("/workspaces/MODP/.env")

from goselect_docproc.contracts import ContentType, Span, WorkItem  # noqa: E402
from goselect_docproc.extractors import ModelExtractor, SegmentContext  # noqa: E402
from goselect_docproc.models import AzureOpenAIModel  # noqa: E402
from goselect_docproc.reconcile import TagLexicon, harvest  # noqa: E402
from goselect_docproc.render import render_page  # noqa: E402
from goselect_docproc.tiling import VisionLimits, plan_tiles  # noqa: E402

PDF = Path("/workspaces/MODP/sample_docs/98796_2_VFDDwgsPlainvilleMATurnpikeLakeWTP.pdf")
PAGE = int(os.getenv("PAGE", "1"))
WANT = {"VFD-401", "RWP-401"}

data = PDF.read_bytes()
deployment = sys.argv[1] if len(sys.argv) > 1 else "gpt-5.6-sol"
dpis = [int(d) for d in (sys.argv[2:] or ["100", "150"])]

# The lexicon the pipeline would now build, harvested from the whole package.
from goselect_docproc.cli import _layout_client  # noqa: E402

layout, _ = _layout_client(Path("/workspaces/MODP/.cache")).analyze(data)
lexicon = TagLexicon(harvest([layout.content]))

item = WorkItem(
    job_id="probe", correlation_id="probe", file_id="f1", file_ordinal=0,
    segment_id=f"f1-p{PAGE}", content_type=ContentType.DRAWING,
    first_page=PAGE, last_page=PAGE, spans=[Span(offset=0, length=0)],
    layout_uri="x", figures=[f"page-{PAGE}"],
)

model = AzureOpenAIModel(
    endpoint=os.environ["AZURE_OPENAI_ENDPOINT"], deployment=deployment,
    max_completion_tokens=16000,
)
limits = VisionLimits.azure_openai()

print(f"page {PAGE} of {PDF.name}, model {deployment}, lexicon {len(lexicon.tags)} tags\n")
for dpi in dpis:
    png = render_page(data, PAGE, dpi)
    width, height = struct.unpack(">II", png[16:24])
    tiles = plan_tiles(width, height, limits)
    approx_tokens = len(tiles) * 765

    extractor = ModelExtractor(
        content_type=ContentType.DRAWING, model=model,
        vision_limits=limits, max_tiles=len(tiles),
    )
    context = SegmentContext(content="", item=item, figures={f"page-{PAGE}": png}, lexicon=lexicon)
    try:
        payload = extractor.extract(context)
    except Exception as exc:  # noqa: BLE001
        print(f"{dpi:>4} dpi  {width}x{height}  {len(tiles):>3} tiles  FAILED {type(exc).__name__}: {str(exc)[:90]}")
        continue

    tags = {(s.tag or "").upper() for s in payload.vfds + payload.motors if s.tag}
    pairs = {(p.vfd_tag, p.motor_tag) for p in payload.pairs}
    hit = WANT & tags
    print(
        f"{dpi:>4} dpi  {width}x{height}  {len(tiles):>3} tiles  ~{approx_tokens/1000:.0f}k vis-tokens  "
        f"tags={len(tags):>3}  VFD-401/RWP-401 found: {sorted(hit) or 'NEITHER'}"
    )
    near = sorted(t for t in tags if "401" in t or t.startswith(("VD", "VFD", "RWP")))
    print(f"        401-family tags read: {near[:12]}")
    linked = [f"{a}<->{b}" for a, b in pairs if a and "401" in str(a)]
    print(f"        pairs on 401: {linked[:6]}")
