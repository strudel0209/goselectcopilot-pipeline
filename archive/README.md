# GoSelect Copilot — producer-agnostic extraction spine

A testable component that turns a mixed-content specification package into
validated, grounded, quotation-ready structured data.

**The change in one line:** the pipeline's unit of work becomes the *segment*
instead of the *file*. Everything else in the existing Azure architecture stays
as drawn.

**The shape in one line:** a fixed **spine** (contracts, span algebra,
reconciliation, tiling, validation, coverage proof, scoring) plus a **swappable
producer** for the OCR/region step — chosen by measurement, not by argument.

```
bytes ──▶ [ SegmentProducer ] ──▶ DocumentAnalysis ──▶ spine ──▶ JobResult
                 ▲
    di-layout · mistral-blocks · content-understanding · hybrid-cu-di
```

```bash
goselect-docproc bench --producers di-layout,mistral-blocks corpus/*.pdf
```

**Client-facing documents:** [docs/PROPOSAL.md](docs/PROPOSAL.md) is the
solution proposal for ABB — the three defects, the fix for each, the test plan
that gates integration, and the phasing. [docs/ALTERNATIVES.md](docs/ALTERNATIVES.md)
is the build-vs-buy second opinion that argues for shipping **one** engine
instead of four.

---

## Producers

All four implement one protocol and are scored on the same gates. The spine is
integrated once; producers are swapped by configuration.

| Producer | Intra-page | Figure crops | Residency | Status | Notes |
|---|---|---|---|---|---|
| `di-layout` | yes | server-side | Azure-native | GA | Zero new dependencies. Page type is an **unmeasured heuristic** |
| `mistral-blocks` | yes | no | Azure sold-direct **or** first-party | **Preview** | Service-labelled blocks replace most of the classification code |
| `content-understanding` | **no** | no | Azure-native | GA | Classify + split from a description, no training data |
| `hybrid-cu-di` | yes | server-side | Azure-native | GA | CU routes, DI supplies geometry. Costs both |

### Verified constraints — `mistral-ocr-4-0` on Azure

From Microsoft Learn, *Foundry Models sold directly by Azure*:

| Capability | Value | Consequence for ABB |
|---|---|---|
| Input | **30 pages / 30 MB** per request | The customer's own table says text specs run **1–40 pp**. Chunking is mandatory, and page offsets must be rebased — implemented and tested here |
| Languages | **`en` only** | The first-party API advertises 170 languages; the Azure-hosted variant does not. A hard filter for non-English packages |
| Tool calling | no | Irrelevant for OCR |
| Status | **Preview** | Not production-committable. `mistral-document-ai-2512` is the non-preview sibling under the same 30-page, `en`-only limits |

This is the same shape of trade-off as Claude on Foundry: the Azure-hosted
variant is more capable on residency and less capable on features. Decide it
deliberately.

### Content Understanding — what it does and does not cover

CU is **GA** (`2025-11-01`) and covers more of this pipeline than the producer
table suggests. Beyond classify-and-split it does field extraction to a nested
schema and returns **page number, bounding box and a 0–1 confidence per field**
when `estimateFieldSourceAndConfidence` is set — since July 2026 for `extract`,
`classify` and `generate` fields alike. That is problem 2's evidence capture as a
service call, and it is why [docs/ALTERNATIVES.md](docs/ALTERNATIVES.md)
recommends CU as the engine rather than as one producer among four.

The structural limit stands:

> *"The minimum unit for classification of documents is a single page.
> Intra-page classification isn't supported."*

On the ABB sample **15 of 20 pages carry more than one content kind and 4 carry
all three**, so this producer reports `intra-page = 0.000` **by construction**.
That is why `hybrid-cu-di` exists.

---

## The three problems this solves

### 1. Multi-type PDFs

Today a package is classified once per file and routed down one of three
branches. A 40-page package containing spec prose, motor schedules and
single-line diagrams gets **one** label, so content that doesn't match it is
sent to the wrong prompt or discarded.

This package splits a file into segments (runs of same-type pages) and then into
intra-page **regions**, so a page holding a schedule *and* an inset diagram is
separated deterministically. On the sample ABB catalogue, **15 of 20 pages carry
more than one content kind and 4 carry all three** — under the current design
every one of those pages is partially mis-routed.

Azure cannot do this for you: *"The minimum unit for classification of documents
is a single page. Intra-page classification isn't supported."* — Content
Understanding classifier docs.

### 2. Context and reference traceability

Section references are misattributed when code falls back to *nearest text above
in reading order*, which is wrong on two-column and landscape pages. This
package indexes every heading by span offset and attributes any element to the
last heading whose offset precedes it — binary search, exact, no model call.

Scanned documents lose paragraph `role`, so there is an explicit geometry
fallback that reports itself as unreliable. Section accuracy **must be reported
separately for digital and scanned**; a blended number hides the failure.

### 3. Reconstructing parallel work

Segments are span ranges over one immutable `content` string, never copies of
it. The global ordering key is `(file_ordinal, span_offset)` — never page
number, which is ambiguous once a page holds several regions. Workers may finish
in any order across any number of queues; reassembly is a sort.

A `coverage` report proves nothing was dropped. On the sample document:
**100.00% accounted for, 0 unexplained characters.**

---

## Install and run

```bash
pip install -e .                       # or: conda env update -f environment.yml
cp .env.example .env                   # DOCUMENTINTELLIGENCE_ENDPOINT
```

```bash
# Segment only. One Layout call per file, no model spend. Safe to repeat.
goselect-docproc segment pkg.pdf --strict

# The exact queue messages that would be published.
goselect-docproc plan pkg.pdf

# Full pipeline.
goselect-docproc run pkg.pdf --markdown

# Vision budget for a drawing, before spending a token.
goselect-docproc tiles 13200 10200
```

Results are cached by content SHA-256, so the same bytes are never analysed —
or billed — twice.

---

## Why drawing tags come back corrupted

The reported defect is `VFD-401` extracted as `$\sqrt{150-401}$`. Two stacked
causes, and neither is an OCR-quality problem:

**a. `ocr.formula` corrupts drawings.** The box drawn around a tag is detected
as a radical sign. This feature is disabled globally and never enabled on a
`DRAWING` segment. Free to fix, no segmentation work required.

**b. The image is downscaled below legibility before the model sees it.** Claude
caps both long edge and visual tokens per tier and silently downscales:

| Tier | Models | Max long edge | Max visual tokens |
|---|---|---|---|
| High-resolution | Claude 4.7 and later | 2576 px | 4784 |
| Standard | all others, **including Claude 4.5** | 1568 px | 1568 |

Measured for an E-size sheet at 300 DPI (13200×10200), 10 pt tag text:

```
standard        whole sheet  scale=0.096  text 41.7px ->  4.0px  TOO SMALL
high-resolution whole sheet  scale=0.167  text 41.7px ->  7.0px  TOO SMALL
high-resolution 48 tiles     scale=1.000  text 41.7px -> 41.7px  OK
```

At 4 px of glyph height, `V→1, F→5, D→0` is the expected outcome. Raising
Document Intelligence's OCR resolution cannot fix it, which is exactly what the
customer observed. The fix is to **tile at native resolution** (`tiling.py`), and
to use a high-resolution-tier model.

**This costs real money.** A fully tiled E-size sheet is ~235,000 visual tokens,
roughly **$1.18 per sheet** at Opus 5 input pricing. Tile around detected content
rather than the whole sheet, and use DI's server-side figure crops
(`output=figures`) to bound the area. The default `max_tiles=40` guard exists to
stop a runaway sheet silently costing more than the quotation is worth.

---

## Design rules the code enforces

| Rule | Where |
|---|---|
| Split the index, not the bytes — one Layout call per file, ever | `layout.py` |
| Geometry is truth; reading order is a guess | `sections.py` |
| Claim order depends on content type; a drawing owns its title block | `regions.py` |
| Deterministic work (units, arithmetic, dedup) never goes to a model | `extractors.parse_quantity`, `assemble.py` |
| Every value carries page, span and section evidence, or is rejected | `validate.py` |
| Homoglyphs are fixed with a lexicon, never with prompting | `reconcile.py` |
| Ambiguity is reported, never guessed | `reconcile.TagLexicon.snap` |
| Disagreement between segments becomes a `Conflict`, not a silent winner | `assemble.merge` |
| Partial failure yields `REVIEW`, never a quietly shortened result | `assemble.merge` |

The LLM schema is **flat, shallow and all-required with explicit nulls**, then
expanded into the nested UI shape in Python. That removes the "Claude omits
nested or null fields" class of bugs and works identically on OpenAI and Claude.

---

## Testing and validation

```bash
pytest -q                 # 88 tests, no Azure dependency, no spend
goselect-docproc bench --producers di-layout,mistral-blocks corpus/*.pdf
python eval/score.py --labels eval/labels --manifests out/manifests
```

The unit suite covers the load-bearing logic: interval subtraction, claim order,
containment, section fallback, order-independent reassembly, duplicate delivery,
merge precedence, tag repair, the vision budget, and every producer's response
mapping. It has already caught two real defects — single-letter motor tags such
as `M-401` were not harvested, and Mistral's plain-text footers were counted as
content loss because furniture was inferred from markup rather than declared.

### Bench-off output on the sample

```
producer  docs fail pages coverage lost intra-page breadcrumb review USD/page
di-layout    1    0    20      1.0    0     0.7500     0.8570 0.1430   0.0100
```

`intra-page` is the share of pages where more than one content kind was found —
the metric that separates a real segmenter from a page classifier.

`eval/score.py` asserts ship gates rather than printing numbers to admire:

| Metric | Gate |
|---|---|
| Page classification macro-F1 | 0.85 |
| Segment boundary IoU | 0.90 |
| Section attribution — digital | 0.95 |
| Section attribution — **scanned** | 0.80 |
| Coverage pass rate | 1.00 |
| VFD–motor pair F1 | 0.90 |

It exits non-zero on failure, so it belongs in CI.

---

## What is proven, and what is not

**Proven on real data** (20-page ABB catalogue): span subtraction, 100% coverage,
order-independent reassembly, section breadcrumbs, per-segment feature flags.

**Proven synthetically only:** figure-absorbs-title-block. Zero containment hits
on the sample, because its figures are illustration panels, not drawing sheets.
**Get a real single-line diagram or P&ID before trusting it.**

**Not yet measured at all:** classification accuracy. `HeuristicClassifier` is a
baseline to bootstrap labels, not production accuracy — on the sample it merged
pages 1–6 into one `DRAWING` segment at confidence **0.177**, which the review
gate correctly flagged. Swap in Content Understanding `contentCategories` or a DI
custom classifier; the `PageLabel` contract means nothing downstream changes.

**The sample is not representative.** It is an ABB product catalogue, not a
customer specification package. Catalogues have consistent templated layout;
customer packages do not. **Hand-label 15–20 real packages before quoting any
number from this repo.**

---

## Two decisions the customer must make

**Claude structured outputs are unavailable on Azure-hosted Foundry
deployments.** Anthropic's documentation is explicit: structured outputs require
a *Hosted on Anthropic* deployment; requests against a *Hosted on Azure*
deployment return `400` by design. That is why forced `tool_choice` is currently
the only option — it is a deployment configuration consequence, not a model
limitation. Switching unlocks `output_config.format`, but *Hosted on Anthropic*
is Global Standard only and inference leaves Azure. Given ABB asked "where must
data reside?", this is a trade-off to decide, not a free upgrade.

**If a DI custom classifier is in use, check `splitMode`.** In `2024-11-30` GA
the default is `none`, so a multi-document PDF returns a single class — precisely
the reported symptom. Passing `splitMode=auto` may resolve part of the problem in
one line.

---

| Document | Audience |
|---|---|
| [docs/PROPOSAL.md](docs/PROPOSAL.md) | ABB. The three defects, the fix for each, the go/no-go test plan, phasing, and the decisions ABB must make |
| [docs/ALTERNATIVES.md](docs/ALTERNATIVES.md) | ABB architecture review. Build-vs-buy against Content Understanding, Mistral OCR 4, Docling, LlamaParse, Reducto and LandingAI ADE |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | The team owning the orchestrator. Wiring into Container Apps / Service Bus / Cosmos |
| [docs/TESTING.md](docs/TESTING.md) | Anyone reproducing the numbers |
