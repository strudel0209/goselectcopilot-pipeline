"""Schedule-branch test.

No document in the customer corpus produces a schedule-dominant page, so there is
no SCHEDULE *segment* to route. 98796 does carry schedule *regions* inside drawing
sheets - the intra-page case - so the branch is exercised against those directly.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, "/workspaces/MODP/src")
from dotenv import load_dotenv

load_dotenv("/workspaces/MODP/.env")

from goselect_docproc.cli import _layout_client  # noqa: E402
from goselect_docproc.contracts import ContentType, Span, WorkItem  # noqa: E402
from goselect_docproc.extractors import ModelExtractor, SegmentContext  # noqa: E402
from goselect_docproc.models import AzureOpenAIModel  # noqa: E402
from goselect_docproc.regions import page_regions  # noqa: E402
from goselect_docproc.spans import text_for  # noqa: E402

PDF = Path("/workspaces/MODP/sample_docs/98796_2_VFDDwgsPlainvilleMATurnpikeLakeWTP.pdf")

result, _ = _layout_client(Path("/workspaces/MODP/.cache")).analyze(PDF.read_bytes())

schedule_spans: list[tuple[int, int]] = []
for page in range(1, len(result.pages) + 1):
    for region in page_regions(result, page, segment_type=ContentType.SCHEDULE):
        if region.kind is ContentType.SCHEDULE:
            schedule_spans.extend(s.as_tuple() for s in region.spans)

text = text_for(result.content, schedule_spans)
print(f"schedule regions: {len(schedule_spans)} spans, {sum(l for _, l in schedule_spans)} chars")
print("--- first 600 chars ---")
print(text[:600])
print("-----------------------\n")

item = WorkItem(
    job_id="probe", correlation_id="probe", file_id="f1", file_ordinal=0,
    segment_id="f1-sched", content_type=ContentType.SCHEDULE,
    first_page=1, last_page=len(result.pages),
    spans=[Span(offset=o, length=l) for o, l in schedule_spans],
    layout_uri="x",
)

for deployment in sys.argv[1:]:
    model = AzureOpenAIModel(
        endpoint=os.environ["CONTENTUNDERSTANDING_ENDPOINT"], deployment=deployment
    )
    extractor = ModelExtractor(content_type=ContentType.SCHEDULE, model=model)
    payload = extractor.extract(SegmentContext(content=result.content, item=item))

    print(f"=== {deployment} ===")
    print(f"  vfds={len(payload.vfds)} motors={len(payload.motors)} pairs={len(payload.pairs)}")
    for v in payload.vfds[:6]:
        print(f"    VFD   {str(v.tag):14s} power={v.power.raw} volt={v.voltage.raw} curr={v.current.raw}")
    for m in payload.motors[:6]:
        print(f"    MOTOR {str(m.tag):14s} power={m.power.raw} volt={m.voltage.raw}")
    for p in payload.pairs[:8]:
        print(f"    PAIR  {p.vfd_tag} <-> {p.motor_tag}")
    print(f"  notes: {payload.notes[:3]}")
