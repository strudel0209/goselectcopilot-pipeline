# Solution Walkthrough

Everything needed to understand, install, test and integrate the segment-level
document processing component — for engineers picking this up cold.

**Contents**

1. [What this solves](#1-what-this-solves)
2. [Architecture](#2-architecture)
3. [Data flow](#3-data-flow)
4. [File-by-file guide](#4-file-by-file-guide)
5. [Infrastructure required](#5-infrastructure-required)
6. [Installation](#6-installation)
7. [Testing with ABB documents](#7-testing-with-abb-documents)
8. [Reading the output](#8-reading-the-output)
9. [Troubleshooting](#9-troubleshooting)
10. [Integration checklist](#10-integration-checklist)

---

## 1. What this solves

Three defects reported by the customer, and the mechanism behind each.

| Reported symptom | Actual mechanism | Fixed in |
|---|---|---|
| Mixed-content PDFs break | The unit of work is the **file**, so one label is applied to a 40-page package | `segmentation.py`, `regions.py` |
| Section references misattributed, worse on scans | Fallback to *nearest text above in reading order*, which is wrong on two-column and landscape pages | `sections.py` |
| Parallel results can't be recombined | No total ordering key; page number is ambiguous once a page holds several regions | `assemble.py` |
| OCR confuses table headers with annotations | DI reports table cells **twice** — once as cells, once as paragraphs; the consumer glues overlapping ranges | `spans.py`, `regions.py` |
| `VFD-401` extracted as `$\sqrt{150-401}$` | (a) `ocr.formula` reads the tag box as a radical; (b) the drawing is downscaled to ~4 px glyph height before the model sees it | `layout.py`, `tiling.py`, `reconcile.py` |

**The change in one sentence:** the pipeline iterates *segments* instead of
*files*. One new state, one loop variable, and Document Intelligence feature
flags move from global to per-segment. Nothing else in the Azure architecture
changes.

---

## 2. Architecture

![Azure architecture](architecture.svg)

Green components are new or changed. Everything else already exists.

**What is genuinely new:**

- `Segmentation_State` — one Layout call per file, then pure post-processing to
  produce `manifest.json` plus a coverage proof.
- `CrossSegment_Reconciliation` — ordering, dedup, tag-lexicon repair, merge
  precedence and conflict detection.
- Service Bus messages are per **segment**, not per **file**.
- Document Intelligence feature flags are per segment.
- CI gains a `pytest` + `eval/score.py` quality gate.

**What is unchanged:** Blob, Service Bus, Container Apps Jobs, Cosmos, Foundry,
Entra, Key Vault, API Management, CI/CD, correlation IDs, concurrency
guardrails, and the three extraction sub-state machines themselves.

---

## 3. Data flow

![Data flow](dataflow.svg)

The single idea worth internalising: **Document Intelligence returns one long
string per file**, and every table, figure and paragraph is described as a
`{offset, length}` range into it. Segments are ranges over that string, never
copies of it. That is why:

- regions can be separated exactly (integers cannot be "nearly" right);
- workers can finish in any order (the ordering key travels with the data);
- coverage is provable (the union of all spans either equals the content or it
  does not).

---

## 4. File-by-file guide

Read in this order. Each module depends only on the ones above it.

### Foundations

#### `spans.py` — 108 lines
The span algebra everything else reduces to. Pure functions, no dependencies.

| Function | Purpose |
|---|---|
| `subtract(spans, claimed)` | Interval **difference**. A later claimant keeps the remainder rather than being dropped whole. |
| `overlaps(a, b)` | Merge-scan overlap test, `O(n+m)`. |
| `gaps(length, spans)` | Everything no span covers — the basis of the coverage proof. |
| `is_benign_gap(text)` | True only for page furniture and structural markup. Separates *dropped by design* from *lost*. |
| `text_for(content, spans)` | Materialise a slice, always in document order. |

> `subtract` returning `[(0,30),(50,50)]` for `[(0,100)] − [(30,20)]` is the
> single most load-bearing behaviour in the package. It has seven tests.

#### `geometry.py` — 62 lines
Bounding-polygon helpers. DI polygons are `[x1,y1,…,x4,y4]` clockwise from
top-left, in **inches** for PDF/Office and **pixels** for images — nothing here
assumes a unit. `contains()` powers "a figure absorbs its title block";
`is_axis_aligned()` detects rotated text on drawing sheets.

#### `contracts.py` — 378 lines
**The integration surface.** Everything crossing a process, queue or service
boundary is a Pydantic model defined here. Read this file first if you are
integrating rather than modifying.

| Model | Role |
|---|---|
| `Span`, `Evidence` | A character range; where a value came from |
| `Region` | A typed slice of one page |
| `Segment` | **The unit of work** |
| `Manifest` | What the orchestrator persists; `sort_key()` defines global order |
| `WorkItem` | The Service Bus body — a pointer, never a payload |
| `SegmentResult` | One Cosmos document; `id` is deterministic |
| `MotorSpec`, `VfdSpec`, `Pair` | Domain payload GoSelect consumes |
| `Coverage`, `Conflict`, `JobResult` | Proof, disagreement, final artefact |

Two layers on purpose: `*Raw`-style flat shapes for the model, domain models for
GoSelect. Expansion between them is deterministic Python.

### Producing segments

#### `producers/base.py` — the swap point
Defines `SegmentProducer`, `DocumentAnalysis`, `ProducerCost` and
`ProducerCapabilities`. **This is the boundary that keeps the solution simple.**
Everything that turns bytes into segments sits behind it; everything that turns
segments into a quotation-ready result sits in front of it and never changes.

A producer must deliver four things:

1. `content` — **one** immutable string per file. A producer that works
   page-by-page must stitch pages and rebase offsets itself.
2. `segments` — with typed, non-overlapping `regions`.
3. `section_index` — for breadcrumbs.
4. `cost` — so the bench-off ranks on price as well as accuracy.

`DocumentAnalysis.coverage()` is the loss proof, computed identically whatever
produced the segments. Note that **furniture is producer-declared**, not sniffed
from markup: DI wraps headers in HTML comments, Mistral returns them as plain
text with a block label.

`ProducerCapabilities.check()` fails loudly on page/size limits and Preview
status rather than letting them be discovered in production.

#### `producers/di_layout.py` — the baseline
Wraps the existing DI profiling, heuristic classification, span subtraction and
section indexing. Azure-native, no residency question, server-side figure crops,
no page cap. Its weakness is the thing the bench-off exists to settle: page type
is a heuristic with **no measured accuracy**.

#### `producers/mistral_blocks.py` — service-labelled blocks
`include_blocks=True` returns paragraph-level boxes already labelled (`text`,
`title`, `list`, `table`, `image`, `equation`, `caption`, `code`, `references`,
`aside_text`, `header`, `footer`, `signature`). Where that labelling is accurate
it replaces most of `segmentation.py` and `regions.py`.

Two implementation details that matter:

- **Offset rebasing.** Mistral returns per-page markdown with per-request
  offsets. This module stitches pages and rebases, because the spine's ordering
  key depends on absolute offsets. Tested explicitly.
- **Page chunking.** The 30-page Azure cap is handled by page-range requests,
  not by splitting the PDF — splitting bytes is the trap that restarts offsets.

`title` blocks build the section index directly, which may prove **better than
DI on scans**, where paragraph roles degrade. The bench-off will say.

#### `producers/content_understanding.py` — the router
Classify and split from a category *description*, no training data. Emits
whole-page regions because intra-page splitting is a documented service limit,
not an implementation gap. Includes the ready-to-`PUT` analyzer definition.

#### `producers/hybrid.py` — CU routes, DI splits
Uses each service for what it is good at: CU for classification, DI for spans,
geometry and figure crops. DI's content stays authoritative; only segment
boundaries and labels are overridden. Costs both services, so it must earn its
place in the bench-off.

#### `bench.py` — the decision procedure
Runs every producer over the same corpus and reports coverage, **intra-page
routing rate**, breadcrumb rate, review rate, cost per page and seconds per
page. A producer that fails is recorded as a result, not an exception.

> `multi_kind_page_rate` is the metric to watch. A producer that cannot split
> inside a page reports `0.000` by construction — on a corpus where most pages
> are mixed, that is disqualifying, not a rounding error.

#### `layout.py` — 152 lines
The only place the Document Intelligence SDK is touched. Cache-first: results
are keyed by content SHA-256, so the same bytes are never analysed or billed
twice. `LayoutOptions` encodes the deliberate feature policy — `FORMULAS` off,
`OCR_HIGH_RESOLUTION` per-segment, `output=["figures"]` for server-side crops.
`figure_png()` fetches those crops and caches them to disk.

#### `segmentation.py` — 193 lines
Page profiling and classification.

- `profile_pages()` derives 17 signals per page (table area ratio, word density,
  rotated-word ratio, short-token ratio, landscape, handwritten…) from the
  response you already paid for.
- `HeuristicClassifier` scores them. **Baseline only** — it exists to prove the
  shape and bootstrap labels.
- `PageClassifier` is a `Protocol`. Swap in Content Understanding
  `contentCategories` or a DI custom classifier and nothing downstream changes.
- `build_segments()` collapses contiguous same-type pages. Segment confidence is
  the **minimum** page confidence in the run — a segment is only as trustworthy
  as its weakest page.

#### `regions.py` — 140 lines
Intra-page splitting. No Azure classifier does this: *"The minimum unit for
classification of documents is a single page."*

Two rules make it exact:
- **Claim order by content type** (`CLAIM_ORDER`). On a `DRAWING` segment
  figures claim first, because the title block and BOM are *tables* that belong
  to the drawing.
- **Containment.** A figure absorbs any table whose bounding box sits inside it.

Everything unclaimed and not page furniture becomes narrative.

#### `sections.py` — 158 lines
The traceability fix. `build_section_index()` indexes every heading by span
offset; `path_for(offset)` binary-searches for the last preceding heading and
expands it into a breadcrumb by level.

Scanned documents lose paragraph `role`, so there is a geometry fallback that
reports `reliable == False`. Note that DI's `ocr.font` add-on returns
`similarFontFamily`, `fontStyle`, `fontWeight`, `color`, `backgroundColor` —
**no font size** — so glyph height comes from the bounding polygon. Bullets and
decorative rules are rejected as headings.

#### `manifest.py` — 158 lines
Assembles the contract. `coverage_for()` produces the loss proof.
`work_items()` turns segments into queue messages and is where feature flags
become per-segment. **File ordinal is frozen here** and must never be
re-derived — it is half the global reassembly key.

### Extraction

#### `tiling.py` — 196 lines
The root-cause fix for corrupted CAD tags, and the module most worth reading.

Claude views images in 28×28 patches and caps both long edge and visual tokens
per tier, silently downscaling anything larger:

| Tier | Models | Max long edge | Max visual tokens |
|---|---|---|---|
| High-resolution | Claude 4.7 and later | 2576 px | 4784 |
| Standard | all others, **including 4.5** | 1568 px | 1568 |

`assess()` quantifies whether tag text survives; `plan_tiles()` covers the sheet
with overlapping native-resolution tiles. `max_tiles` guards against runaway
cost.

#### `extractors.py` — 308 lines
The three branches behind one protocol. `ModelClient` is the provider seam —
implement once per Foundry deployment. `NullModel` runs the whole pipeline with
zero spend, which is what makes CI possible.

Schema discipline, applied identically to every provider: flat, shallow, every
property `required`, absence as explicit `null`, `additionalProperties: false`.
`expand()` converts that into the nested domain shape in Python.
`parse_quantity()` handles units — never a model's job.

#### `reconcile.py` — 155 lines
Cross-segment tag repair. `VFD-401` misread as `150-401` is **edit distance 3**,
so fuzzy matching cannot reach it safely. Instead each character maps to an OCR
confusion class (`0ODQ`, `1ILV|`, `5SF`, …) and both strings collapse to the
same signature — an exact, explainable match.

Both mechanisms **refuse on ambiguity**. `VFD-101` scores 86% against `VFD-401`;
rewriting it would be silent data loss, so it is left alone.

#### `validate.py` — 87 lines
Two gates: domain plausibility envelopes (power, voltage, current, frequency,
pole count, with HP and kV conversion) and **grounding** — a value with no
page/span evidence is rejected, not trusted.

### Merging and orchestration

#### `assemble.py` — 230 lines
- `document_order()` — sort by `(file_ordinal, span_offset)`.
- `completion()` — counts **distinct** terminal segment documents, which is what
  makes at-least-once delivery harmless.
- `merge()` — merges same-tag specs with stated precedence, dedups pairs, raises
  confidence on corroboration, and records disagreement as a `Conflict` rather
  than silently picking a winner.
- `reassemble_markdown()` — audit projection only.

#### `pipeline.py` — 198 lines
Reference implementation of the state machine, mapping 1:1 to the customer's
states. Concurrency is a thread pool here and queue depth in production; nothing
in the logic depends on which. Retries use exponential backoff and record
partial failure rather than raising.

#### `cli.py` — 223 lines
Four commands: `segment`, `plan`, `run`, `tiles`. Loads `.env`, and falls back
to cache-only mode when no endpoint is configured — which is what lets an eval
set replay in CI with no credentials.

### Tests and evaluation

| File | Covers |
|---|---|
| `tests/conftest.py` | Duck-typed DI fakes — no Azure dependency |
| `tests/test_spans.py` | Interval algebra, coverage, benign-gap classification |
| `tests/test_regions_sections.py` | Subtraction, claim order, containment, heading fallback |
| `tests/test_assemble.py` | Order independence, duplicate delivery, merge precedence, partial failure |
| `tests/test_reconcile_tiling.py` | Tag repair, ambiguity refusal, vision budget vs published tables |
| `tests/test_producers.py` | Producer response mapping, offset rebasing, page chunking, declared limits |
| `eval/score.py` | Ship gates against hand labels; exits non-zero on failure |

---

## 5. Infrastructure required

### To evaluate (what you need this week)

| Resource | SKU | Why |
|---|---|---|
| Azure AI Document Intelligence | S0, any region with v4.0 | `prebuilt-layout`, `2024-11-30` |
| Workstation | Python 3.11+ | Everything else runs locally |

That is the entire footprint for `segment`, `plan` and the whole test suite. The
`run` command works with `NullModel` and no model deployment at all.

### To run extraction

| Resource | Notes |
|---|---|
| Azure AI Foundry project | One deployment per model you benchmark |
| Claude deployment | **Opus 5 or 4.7+** for drawings — 4.5 is standard tier and cannot resolve tag text |
| GPT deployment | For text and schedule branches |

### To integrate into production

| Layer | Component | Configuration that matters |
|---|---|---|
| Entry | Front Door / WAF, API Management, Entra ID | Existing |
| Orchestration | Container Apps | Runs `Pipeline.segment` |
| Queue | Service Bus | Messages are ~400 bytes; **enable sessions only if you need per-file ordering** — you do not, the sort key handles it |
| Workers | Container Apps Jobs | Scale on queue depth; set max replicas to your Foundry TPM budget |
| State | Cosmos DB | Partition key `/job_id`; **change feed** drives completion |
| Artefacts | Blob Storage | Immutable container for `layout.json`, `layout.md`, `figures/` |
| Secrets | Managed Identity + Key Vault | No keys in code |
| Telemetry | App Insights + Log Analytics | `correlation_id` as a custom dimension |

### RBAC

The worker identity needs:

| Role | Scope |
|---|---|
| `Cognitive Services User` | Document Intelligence + Foundry resource |
| `Storage Blob Data Contributor` | Artefact container |
| `Cosmos DB Built-in Data Contributor` | Job database |
| `Azure Service Bus Data Receiver` | Processing queue |

### Cost model

| Item | Driver | Note |
|---|---|---|
| Layout | Pages, **once per unique file** | SHA-256 cache means reprocessing is free |
| `ocr.highResolution` | Pages, premium add-on | **Drawing segments only** — applying it globally is money burned on prose |
| Text / schedule LLM | Tokens | Segments are smaller than files, so prompts shrink |
| Drawing vision | Visual tokens | The one to watch — see below |

**Vision cost is the risk.** A fully tiled E-size sheet at 300 DPI is roughly
**235,000 visual tokens ≈ $1.18/sheet** at Opus 5 input pricing. Bound it by
tiling DI's figure crops rather than whole sheets, and keep the `max_tiles`
guard. Check your own numbers before committing:

```bash
goselect-docproc tiles 13200 10200
```

---

## 6. Installation

### 6.1 Prerequisites

```bash
python --version      # 3.11 or newer
az --version          # for Entra sign-in
```

### 6.2 Get the code and dependencies

```bash
git clone <repo> && cd MODP
pip install -e ".[dev]"
```

If PyPI is blocked by a corporate firewall (a `SSLV3_ALERT_HANDSHAKE_FAILURE`
against `files.pythonhosted.org` is the signature), use conda-forge instead:

```bash
conda env create -f environment.yml
conda activate goselect
```

### 6.3 Configure

```bash
cp .env.example .env
```

```ini
DOCUMENTINTELLIGENCE_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
# Leave the key unset to use Entra ID, which is the recommended path.
# DOCUMENTINTELLIGENCE_API_KEY=
```

```bash
az login --tenant <tenant-id>
```

The `Cognitive Services User` role on the Document Intelligence resource is
required for `DefaultAzureCredential` to work.

### 6.4 Verify the install — no Azure calls, no spend

```bash
pytest -q
```

Expected:

```
73 passed
```

If this passes, the algebra, subtraction, ordering, merge, tag repair and vision
budget are all sound on your machine.

### 6.5 Verify Azure connectivity

```bash
goselect-docproc tiles 13200 10200     # pure arithmetic, no network
goselect-docproc segment sample_docs/mixed_package.pdf
```

The second command makes exactly **one** Layout call and caches it under
`.cache/`. Re-running is free.

---

## 7. Testing with ABB documents

### Step 1 — Assemble a representative corpus

Not a demo set. Aim for **15–20 real customer specification packages**,
stratified:

| Stratum | Minimum | Why |
|---|---|---|
| Digital-born PDFs | 6 | The easy case; establishes the ceiling |
| **Scanned packages** | 6 | Where section attribution breaks; must be scored separately |
| Packages containing **CAD / single-line diagrams** | 5 | The tag-corruption case |
| Multi-file packages | 3 | Exercises `file_ordinal` ordering |
| Known-difficult / previously wrong | all you have | These are the ones that matter |

> The `sample_docs/mixed_package.pdf` in this repo is an ABB **product
> catalogue**, not a customer package. Catalogues have consistent templated
> layout; customer packages do not. **Nothing measured on it generalises.**

```bash
mkdir -p corpus && cp /path/to/packages/*.pdf corpus/
```

### Step 2 — Segment everything and read the coverage report

```bash
for f in corpus/*.pdf; do
  goselect-docproc segment "$f" --out "out/$(basename "$f" .pdf)" --strict
done
```

`--strict` exits non-zero if any content was dropped that is not page furniture.
**Investigate every failure before going further** — content lost here can never
be recovered downstream.

Expected shape of a healthy report:

```
coverage
  f1: accounted 100.00% (claimed 34600, furniture 2751, unexplained 0) [ok]
```

### Step 3 — Eyeball the segmentation

Open `out/<name>/manifest.json` and check three things:

1. Do segment boundaries match where the document actually changes type?
2. Are `section_root` breadcrumbs plausible?
3. Which segments were routed `REVIEW`? Low confidence is the classifier telling
   you it does not know — that is correct behaviour, not a bug.

On the bundled sample, `f1-seg-001` merges pages 1–6 at confidence **0.177** and
is flagged `REVIEW`. That is the heuristic failing honestly.

### Step 4 — Hand-label the corpus

One JSON per document in `eval/labels/<name>.json`:

```json
{
  "document": "cust-pkg-001.pdf",
  "scan_quality": "scanned",
  "pages": { "1": "TEXT", "2": "TEXT", "7": "SCHEDULE", "9": "DRAWING" },
  "sections": { "7": "5.1 Motor schedule", "9": "6. Single line diagrams" },
  "tags": ["VFD-401", "M-401", "VFD-721"],
  "pairs": [["VFD-401", "M-401"]]
}
```

Budget roughly 20–30 minutes per package. **This is the actual bottleneck of the
whole project**, and there is no way around it — without labels no claim about
accuracy is measurable.

### Step 5 — Score

First run the **bench-off** to choose a producer, then score the winner.

```bash
goselect-docproc bench corpus/*.pdf \
  --producers di-layout,mistral-blocks,hybrid-cu-di --out out/bench
```

```
producer   docs fail pages coverage lost intra-page breadcrumb review USD/page s/page
di-layout     1    0    20      1.0    0     0.7500     0.8570 0.1430   0.0100 0.0640
```

Read it in this order:

1. **`lost` must be 0.** Any content loss disqualifies a producer outright.
2. **`intra-page`** — can it separate a schedule from an inset diagram? A
   page-level producer reports `0.000` here by construction.
3. **`breadcrumb`** — share of segments that resolved a section path. This is the
   traceability metric, and it must be read separately for scanned documents.
4. **`review`** — share of segments the producer could not classify confidently.
   High is honest, not broken; it costs human time rather than correctness.
5. **`USD/page` and `s/page`** — only after the above.

Unconfigured producers are skipped with a message rather than failing the run, so
you can start with one and add others as credentials arrive.

Then score the chosen producer against the labels:

```bash
python eval/score.py --labels eval/labels --manifests out/manifests --jobs out/jobs
```

| Gate | Threshold | Meaning if it fails |
|---|---|---|
| `page_classification_f1` | 0.85 | Replace the heuristic with Content Understanding |
| `segment_boundary_iou` | 0.90 | Segments are cut in the wrong place; nothing downstream is trustworthy |
| `section_attribution_digital` | 0.95 | Heading detection needs work |
| `section_attribution_scanned` | 0.80 | Geometry fallback is insufficient for your scans |
| `coverage_pass_rate` | 1.00 | **Content loss. Stop and fix.** |
| `pair_f1` | 0.90 | Extraction or pairing logic |

Exit code is non-zero on failure, so this belongs in CI.

The harness warns explicitly if your eval set contains no scanned documents — a
digital-only score is not evidence for the problem the customer actually
reported.

### Step 6 — Test the drawing path specifically

```bash
# What the vision model will actually receive
goselect-docproc tiles 13200 10200
```

```
standard        whole sheet  scale=0.096  text 41.7px →  4.0px  TOO SMALL
high-resolution whole sheet  scale=0.167  text 41.7px →  7.0px  TOO SMALL
high-resolution 48 tiles     scale=1.000  text 41.7px → 41.7px  OK
```

Substitute your real drawing dimensions. If `TOO SMALL` appears for the whole
sheet, no amount of Document Intelligence OCR resolution will fix tag
extraction — which is exactly what the customer observed.

Then run the A/B that proves `ocr.formula` is the second cause:

```bash
goselect-docproc segment drawing.pdf                       # baseline, formulas off
# then re-run with FORMULAS enabled and diff the LaTeX fragment count
```

### Step 7 — Full pipeline

```bash
goselect-docproc run corpus/pkg-001.pdf --markdown --workers 4
```

Produces `manifest.json`, `work_items.json`, `results.json`, `job.json` and
optionally `reassembled.md`.

---

## 8. Reading the output

### `manifest.json`
The contract. Check `coverage[*].unexplained_chars == 0` first — everything else
is meaningless if content was dropped.

### `work_items.json`
Exactly what would go on Service Bus. Confirm `high_resolution: true` and
`formulas: false` on every `DRAWING` item.

### `job.json`

```json
{
  "status": "REVIEW",
  "segments_expected": 7, "segments_done": 6, "segments_failed": 1,
  "conflicts": [
    { "field": "M-401.power", "values": ["75.0", "90.0"],
      "origins": ["TEXT", "SCHEDULE"] }
  ],
  "review_required": ["f1-seg-001", "M-401.power"]
}
```

- `status: REVIEW` is **not** a failure. It means the pipeline found something a
  human should look at rather than guessing.
- `conflicts` are disagreements between segment types. Precedence resolved the
  value, but the disagreement is recorded — never silently discarded.
- `partial: true` means holes exist. A silently shortened result is far more
  dangerous than an obviously incomplete one.

### Evidence
Every extracted value carries `{file_id, page, spans, polygon, section_path,
verbatim, source}`. If a quotation is later disputed, this is how you trace the
number back to the pixel it came from.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `SSLV3_ALERT_HANDSHAKE_FAILURE` on `files.pythonhosted.org` | Corporate firewall allowlists `pypi.org` but not the download host | Allowlist `files.pythonhosted.org`, or use conda-forge |
| `cache miss … and no Document Intelligence client configured` | `DOCUMENTINTELLIGENCE_ENDPOINT` unset and the file is not cached | Set the endpoint, or pre-populate `.cache/` |
| `403 Forbidden` from Document Intelligence | Missing RBAC | Grant `Cognitive Services User` |
| `unexplained_chars > 0` | Segmentation is dropping real content | Read `unexplained_samples`; usually a new markup pattern for `spans.is_benign_gap` |
| All pages classified `OTHER` | Heuristic weights tuned for inch-unit A/Letter pages | Retune `HeuristicClassifier.score`, or swap in a real classifier |
| Segment confidence near zero | The classifier genuinely cannot tell | Correct behaviour — route to review |
| `400 Bad Request` on Claude structured outputs | Deployment is **Hosted on Azure**, where structured outputs are unsupported by design | Use forced tool use, or move to a Hosted-on-Anthropic deployment (Global Standard only, inference leaves Azure) |
| Drawing tags still corrupted | Standard-tier model, or whole sheet sent untiled | Move to Claude 4.7+/Opus 5 and tile at native resolution |
| `N tiles exceeds max_tiles` | Sheet too large to tile whole | Tile DI figure crops instead of the full page |

---

## 10. Integration checklist

**Same-day, no risk**

- [ ] Disable `ocr.formula` on drawing pages
- [ ] If a DI custom classifier is in use, verify `splitMode=auto` — the
      `2024-11-30` default is `none`, which returns a single class for a
      multi-document PDF and is precisely the reported symptom
- [ ] Check whether the Claude deployment is Hosted on Azure

**Before writing integration code**

- [ ] Corpus of 15–20 real packages assembled, including scanned
- [ ] Labels written
- [ ] `eval/score.py` passing, or failures understood and accepted

**Wiring**

- [ ] `Segmentation_State` added ahead of `Extraction_Orchestration_State`
- [ ] `for each <type> file` → `for each <type> segment`
- [ ] Cosmos id set to `{jobId}:{fileId}:{segmentId}`; upsert, never insert
- [ ] Completion driven by **distinct terminal documents**, not a counter
- [ ] Change feed triggers `CrossSegment_Reconciliation`
- [ ] `file_ordinal` frozen at job creation
- [ ] DI feature flags moved from global to per-segment
- [ ] Figure crops persisted to Blob during segmentation, not re-fetched later
- [ ] `correlation_id` propagated to every span and log
- [ ] Alert configured on `coverage.unexplained_chars > 0`

**Before production**

- [ ] Review queue and UI exist for `status: REVIEW` and `conflicts`
- [ ] Cost per package measured on the real corpus, not estimated
- [ ] A real single-line diagram added as a test fixture — the
      figure-absorbs-title-block rule is currently proven synthetically only

---

## Related documents

- [README.md](../README.md) — summary, evidence, open decisions
- [INTEGRATION.md](INTEGRATION.md) — state mapping, storage layout, message
  contracts, rollout plan
