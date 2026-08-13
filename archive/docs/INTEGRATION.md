# Integration guide

For the team owning the GoSelect Copilot orchestrator. This package is a
**library plus a reference orchestrator**, not a replacement runtime. Adopt the
logic; keep Container Apps Jobs, Service Bus and Cosmos exactly as they are.

---

## 1. What changes in the state machine

```
Initialise
  └─ Segmentation_State                    <-- NEW
       └─ Extraction_Orchestration_State
            ├─ for each TEXT segment      -> Get MD     -> LLM (APP / VFD / MOTOR)
            ├─ for each SCHEDULE segment  -> Get Table  -> LLM (PAIR)
            └─ for each DRAWING segment   -> Get Tiles  -> LLM (PAIR, vision)
       └─ JSON_Validation_State
       └─ CrossSegment_Reconciliation      <-- NEW
       └─ Save_Results
```

**Unchanged:** Blob, Service Bus, Container Apps Jobs, Cosmos, Foundry, API
Management, correlation ID, CI/CD, concurrency guardrails, and the three
extraction sub-state machines themselves.

**Changed:** one new state, `for each <type> file` becomes
`for each <type> segment`, and Document Intelligence feature flags move from
global to per-segment.

| This package | Your state |
|---|---|
| `Pipeline.segment` | `Segmentation_State` |
| `Pipeline.plan` | publish to Service Bus |
| `Pipeline.build_lexicon` | `Segmentation_State` tail |
| `Pipeline.run_segment` | Container Apps Job worker |
| `Pipeline.finish` | `CrossSegment_Reconciliation` + `Save_Results` |

---

## 2. Storage layout

Write once, never mutate. This is what makes reassembly a sort rather than a
merge.

```
jobs/{jobId}/manifest.json
jobs/{jobId}/files/{fileId}/source.pdf
jobs/{jobId}/files/{fileId}/layout.json     <- full AnalyzeResult
jobs/{jobId}/files/{fileId}/layout.md       <- result.content, canonical
jobs/{jobId}/files/{fileId}/figures/{figureId}.png
```

Cosmos holds job state and one document per segment result. Nothing large goes
into Cosmos; nothing mutable goes into Blob.

---

## 3. Queue message — a pointer, not a payload

`WorkItem.model_dump(mode="json")` is the Service Bus body:

```json
{
  "schema_version": "1.0.0",
  "job_id": "…", "correlation_id": "…",
  "file_id": "f1", "file_ordinal": 0,
  "segment_id": "f1-seg-003",
  "content_type": "DRAWING",
  "first_page": 8, "last_page": 9,
  "spans": [{"offset": 48210, "length": 3122}],
  "section_root": "3. Scope of Supply > 3.2 Motors",
  "layout_uri": "jobs/…/files/f1/layout.json",
  "figures": ["8.1", "9.1"],
  "high_resolution": true,
  "formulas": false
}
```

The worker fetches the slice it needs. Messages stay far below Service Bus size
limits regardless of document size, and the queue never carries content.

---

## 4. Idempotency and completion

Service Bus is at-least-once. The same segment **will** be processed twice under
retry or lock expiry.

- **Deterministic Cosmos id:** `{jobId}:{fileId}:{segmentId}` (`WorkItem.dedupe_id`).
  Redelivery becomes an upsert.
- **Never increment a counter for progress.** Count *distinct* terminal segment
  documents — `assemble.completion()` does exactly this and is unit-tested
  against duplicate delivery.
- **Trigger reconciliation from the Cosmos change feed** when
  `distinct(DONE) + distinct(FAILED) == manifest.expected_units`. No polling, and
  it survives worker restarts.

Suggested Cosmos partition key: `/job_id`. All reconciliation reads are
job-scoped, so this keeps merge a single-partition query.

---

## 5. Ordering and reassembly

```python
manifest.sort_key(segment)   # -> (file_ordinal, span_offset)
```

- **`file_ordinal` is frozen at job creation** and must never be re-derived.
  Upload order is the contract.
- **Never sort by page number.** Intra-page regions share a page number, so it is
  not a total order.
- `assemble.reassemble_markdown()` exists for audit and chat context only. The
  deliverable to GoSelect is JSON; do not build a markdown re-assembler for a
  consumer that does not exist.

---

## 6. Merge precedence — state it, don't imply it

| Kind of value | Precedence | Reason |
|---|---|---|
| Numeric specifications | SCHEDULE > TEXT > DRAWING | grids are authoritative |
| Pairing / topology | DRAWING > SCHEDULE > TEXT | the diagram shows the wiring |

Disagreement produces a `Conflict` and routes to review. It is never silently
resolved. Corroboration across segment types raises pair confidence by up to
0.3; it never invents a pair.

---

## 7. Per-segment Document Intelligence flags

| Segment | `ocr.highResolution` | `ocr.formula` | Rationale |
|---|---|---|---|
| TEXT | off | off | digital prose |
| SCHEDULE | off | off | grid structure already recovered |
| DRAWING | **on** | **OFF** | high-res helps annotations; formula corrupts them |

Applying `ocr.highResolution` to a whole package is money spent on prose pages.
Turning `ocr.formula` off for drawings is a same-day change with no segmentation
work and pure upside.

---

## 8. Model layer

`extractors.ModelClient` is the only seam a provider touches:

```python
class ModelClient(Protocol):
    name: str
    def complete_json(self, *, prompt, schema, images=None) -> dict: ...
```

Implement once per Foundry deployment. `NullModel` runs the whole pipeline with
zero spend, which is what makes CI possible.

Schema rules, applied to every provider:

- every property `required`, absence expressed as explicit `null`;
- `additionalProperties: false`;
- flat and shallow — expand to the nested UI shape in Python.

Claude-specific, if you stay on forced tool use: property ordering places
required properties first, so marking everything required makes ordering
deterministic for UI mapping. Watch the published caps — **24 optional
parameters** and **16 union-typed parameters** per request — which is a likely
cause of the current schema failures.

---

## 9. Observability

Propagate `correlation_id` from upload through queue, worker, DI call, model
call, validation and the GoSelect callback. `SegmentResult` already carries
`model`, `latency_ms`, `input_tokens`, `output_tokens` and `attempts` — emit
these as App Insights custom dimensions keyed by `job_id` and `segment_id`.

Alert on:

- `coverage.unexplained_chars > 0` — segmentation is dropping content;
- segments routed `REVIEW` as a share of total — classifier drift;
- tiles per drawing above the `max_tiles` guard — runaway vision cost.

---

## 10. Rollout

1. **Week 1 — no risk.** Turn `ocr.formula` off for drawings. Check
   `splitMode=auto` if a DI custom classifier is in use. Run
   `goselect-docproc segment --strict` over the existing corpus and read the
   coverage report.
2. **Week 2 — evidence.** Hand-label 15–20 *real customer packages*, including
   scanned ones. Run `eval/score.py`. This is the go/no-go gate.
3. **Week 3 — shadow.** Run segmentation alongside the current pipeline, writing
   manifests but not consuming them. Compare extraction against today's output.
4. **Week 4 — cut over** the loop variable, one document type at a time, with the
   review threshold set high and falling as evidence accumulates.
5. **Later — swap the producer.** Replace `HeuristicClassifier` with Content
   Understanding `contentCategories`. The manifest contract does not change.

---

## 11. Known limitations

- **Figure crops need the originating analyze result.** DI serves crops from
  `/analyzeResults/{resultId}/figures/{figureId}`, which is only valid while the
  service retains the result. `LayoutClient` caches crops to disk on first fetch;
  in production, persist them to Blob during `Segmentation_State` rather than
  relying on a later re-fetch.
- **`HeuristicClassifier` has no measured accuracy.** Treat every segment count
  in this repo as illustrative.
- **Figure-absorbs-title-block is unexercised on real data.** Add a real drawing
  as a test fixture before relying on it.
- **Tiling cost is significant** (~235k visual tokens for a full E-size sheet).
  Bound it with DI figure crops and content-aware tiling before production.
- **The heading geometry fallback only recognises numbered headings**, and
  reports `reliable == False` so callers can route scanned documents to review.
