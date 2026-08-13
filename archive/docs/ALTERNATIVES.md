# Second opinion — is there a simpler way?

**The question.** The proposed solution is a segmentation spine plus a swappable
extraction engine. That is real complexity. Does a managed product already do
this, and would ABB be better served buying it than building it?

**Short answer.** Yes, partially, and the recommendation changes because of it.
About 60% of what this repository does is now available as a managed Azure
service. The remaining 40% is not available from *any* vendor, managed or open
source, because it is specific to mixed-content engineering packages. The right
posture is **buy the engine, build the four things nobody sells** — not build all
of it, and not assume a product covers it all.

**Verified as of this review.** Everything below is sourced; sources in §8. This
market moved substantially in the first half of 2026 and any comparison older
than that is stale.

---

## 1. Scoring criteria

A candidate is judged only on whether it solves ABB's actual problems.

| # | Criterion | Why it is decisive |
|---|---|---|
| C1 | **Splits a multi-type package**, at page level | Removes ABB's per-file classifier |
| C2 | **Separates content kinds inside one page** | 15 of 20 sample pages carry more than one kind |
| C3 | **Per-field grounding** — page, box, confidence | Problem 2, and the review UI |
| C4 | **Section-reference attribution** — "§3.2 Motors" | Problem 2 as ABB stated it |
| C5 | **Reads E-size CAD sheets at legible resolution** | The corrupted-tag defect |
| C6 | **Extracts to a deep nested schema** (107 fields, 4 levels) | The GoSelect contract |
| C7 | **Azure-native residency** | ABB asked "where must data reside?" |
| C8 | **Production status** — GA, SLA, retirement policy | It goes into a quotation path |

---

## 2. Managed candidates

### 2.1 Azure AI Content Understanding — **recommended engine**

| Criterion | Verdict |
|---|---|
| C1 split | **Yes.** `contentCategories` + `enableSegment`, categories defined by *description* — no labelled training data. Returns page ranges per identified type, and each category can be routed to its own analyzer |
| C2 intra-page | **No.** Documented: *"The minimum unit for classification of documents is a single page. Intra-page classification isn't supported."* |
| C3 grounding | **Yes.** `estimateFieldSourceAndConfidence` (analyzer) / `estimateSourceAndConfidence` (field) returns page number, bounding box and a 0–1 confidence. Since July 2026 this covers `extract`, `classify` **and** `generate` fields |
| C4 sections | **No.** Grounding is spatial, not structural |
| C5 CAD | **Partial.** Figure analysis exists; native-resolution tiling of an E-size sheet does not |
| C6 schema | **Yes.** Nested groups, tables and fixed tables; 1,000-field limit against ABB's 107 |
| C7 residency | **Yes.** Azure-native, bring-your-own Foundry model deployment (`gpt-5.x` family), including Data Zone and PTU |
| C8 status | **GA** since API `2025-11-01`. Async limits 200 MB / 300 pages — comfortably above ABB's stated 1–40 pp text specs. 1,000 pages/min |

**What this removes from the build:** the per-file classifier, the three separate
prompt flows, the JSON-schema plumbing, per-field confidence scoring, most of the
evidence-capture machinery, and the model-governance layer.

**What it does not remove:** C2, C4, C5, and cross-region precedence.

Content Understanding **Pro mode** additionally offers reasoning, multiple input
documents in one call, and an external knowledge base for linking and validation
— which maps closely onto cross-document reconciliation and onto validating
extracted parts against ABB's own product catalogue. It is **preview**, documents
only. Worth an arm in the bench; not worth committing a production path to yet.

### 2.2 Mistral OCR 4 / Mistral Document AI

Released 23 June 2026 and a genuine step change: it returns a *structured*
document — bounding box per block, **block type classification**, per-page and
per-word confidence — rather than flat text. Block types would replace a large
part of the classification code outright.

The problem is the Azure-hosted variant.

| Capability | First-party API | **Azure Foundry (sold directly)** |
|---|---|---|
| Languages | 170 | **`en` only** |
| Pages per request | up to 1,000 | **30 pages / 30 MB** |
| Status | GA | `mistral-ocr-4-0` **Preview**; `mistral-document-ai-2512` non-preview |
| Price | $4 / 1k pages ($2 batch) | $4 / 1k pages Global; $4.4 Data Zone (US/EU). Annotated: $5 / $5.5 |
| Residency | Leaves Azure | Azure-native |

The 30-page cap is disqualifying on its own for packages that run to 40+ pages
without chunking and page-offset rebasing — which reintroduces exactly the
offset-management complexity this design is trying to contain. The English-only
constraint is a hard filter if ABB's customers submit non-English packages.
There is also a reported failure mode where `document_annotation` returns null
while the markdown output is fine, which is a poor property for a contract-bound
extraction step.

Note also the churn: `Mistral-document-ai-2505` retires 20 July 2026 and
`Mistral-ocr-2503` retired 30 January 2026 — three model generations in roughly
15 months. That is a real operational cost for a quotation-path dependency.

**Verdict.** Strong technology, currently the wrong deployment. Keep as a bench
arm, re-evaluate when the Azure variant reaches parity on pages and languages.

### 2.3 Azure AI Document Intelligence Layout

Not a competitor to Content Understanding — a complement, and the only way to get
C2.

It supplies paragraph roles, table cell geometry, figure regions, server-side
figure crops and spans over a single canonical content string. That is precisely
the raw material intra-page span subtraction needs. It supplies nothing for C3
or C6.

**One free fix lives here.** If ABB uses a DI custom classifier, v4.0 GA
(`2024-11-30`) defaults `splitMode` to `none`, which returns **one class for a
multi-document PDF** — an exact match for the reported symptom. Setting
`splitMode=auto` is a one-parameter change.

### 2.4 LlamaParse, Reducto, LandingAI ADE

All three now offer per-field citations with page and bounding box, which is C3
solved. On the LlamaIndex-published ParseBench (treat vendor benchmarks with
care), Reducto and LlamaParse score above Azure Document Intelligence on overall
parse quality; LandingAI ADE is specifically strong on spatially complex forms,
tables and diagrams, which is the closest published match to ABB's drawings.

None of them solve C2 or C4. All of them fail C7 outright — they are not Azure
services, and ABB has explicitly raised residency. For a quotation pipeline
containing customer specifications, that is likely to end the conversation before
accuracy is discussed.

**Verdict.** Use one as a **control arm** in the bench to calibrate how much
accuracy the residency constraint costs. That number is genuinely useful to ABB.
Do not propose one as the production path without an explicit residency waiver.

---

## 3. Open-source candidates

| Tool | What it gives | Why it does not replace the engine |
|---|---|---|
| **Docling** (IBM → Linux Foundation, ~37k stars) | Unified `DoclingDocument` with layout, reading order, table cell boundaries, formula and image placement across PDF/DOCX/PPTX/XLSX. `Granite-Docling-258M` under Apache 2.0 | Excellent structural parser, no field extraction, no confidence, no grounding-to-schema. You would build C3 and C6 yourself, plus run and scale GPU inference |
| **MinerU** | Pipeline / VLM / hybrid backends, LLM-ready markdown+JSON | Same gap, plus self-hosted GPU operations |
| **Marker** | Strong on messy scans with `--use_llm` | Same gap. CPU is slow past ~20 pages |
| **huridocs/pdf-document-layout-analysis** | Dockerised page **segmentation and classification** into text/title/picture/table | The closest OSS analogue to the segmentation layer, and a reasonable fallback if Azure classification underperforms. Still no extraction, grounding or schema |

**Verdict.** Open source is the right answer to *"how do I get structure out of a
PDF"* and the wrong answer to *"how do I get a validated, grounded, contract-shaped
quotation input"*. Adopting one moves work from an Azure bill to an ABB GPU fleet
and an ABB on-call rota, and it does not shrink the four gaps.

---

## 4. What nobody sells

After reviewing every candidate, these four remain unsolved by any product:

| Gap | Why no vendor covers it |
|---|---|
| **C2 — intra-page separation** | Every managed classifier's minimum unit is a page. This is a documented architectural limit, not a maturity gap |
| **C4 — section breadcrumbs** | Grounding answers *where on the page*. "Which clause of the specification" requires a heading index over the document, and nothing exposes one |
| **C5 — native-resolution CAD reading** | Every vision path downscales to a token budget. Reading a 13200×10200 sheet legibly is a tiling and cost-governance problem, not a model problem |
| **Cross-region precedence and conflict surfacing** | "A schedule outranks a drawing for a kW rating, a drawing outranks a schedule for wiring" is ABB domain policy. No product knows it |

Together these are a few hundred lines of deterministic, unit-testable code with
no service dependency. That is a proportionate amount of custom work, and it is
the part that carries ABB's actual domain knowledge — the part worth owning.

---

## 5. The simplification this review actually recommends

Three concrete reductions against the current design:

### 5.1 Ship one engine, not four

The repository currently carries four interchangeable extraction engines. That is
correct as a **selection activity** and wrong as a **product**. Four engines is
four code paths, four failure modes and four sets of API drift.

Run the bench, publish the table, **then delete the losers**. Shipping optionality
is shipping maintenance.

### 5.2 Test whether intra-page routing is still necessary

The three-flow architecture was designed around models that could not read a page
containing prose, a grid and an inset diagram in one pass. Current multimodal
models largely can. It is entirely possible that C2 — the most complex part of
this design — is solving a problem that expired.

Settle it with the A/B in [PROPOSAL.md](PROPOSAL.md) §4.2 and ship the page-level
arm unless intra-page wins by a material margin on pair F1. If it does not win,
a large fraction of the custom code is never written.

### 5.3 Fix the contract before building anything

`vfd_motor_schema_v1_0.json` has 107 fields and **no provenance field**. No
amount of grounding machinery matters if the output contract has nowhere to put
the result. Adding a `source` block is a schema change, costs nothing, and is a
prerequisite for every traceability claim in the proposal.

Likewise, asking a model to emit a 4-level, 107-field object directly is a known
source of dropped nested and null fields. Ask for a flat, all-required, explicitly
nullable shape and expand it in Python. That is a prompt-and-mapping change, not
architecture.

---

## 6. Recommendation

| Layer | Decision |
|---|---|
| Classification, splitting, field extraction, grounding, confidence, model governance | **Buy** — Azure AI Content Understanding (GA) |
| Page geometry for intra-page separation | **Buy** — Document Intelligence Layout, only if the A/B justifies it |
| Intra-page separation, section breadcrumbs, drawing tiling, merge precedence | **Build** — deterministic, testable, ABB-owned domain logic |
| Everything else | **Do not build** |

This is materially simpler than the current design: one engine instead of four, a
managed service doing the work the spine currently duplicates, and a custom layer
scoped to four clearly-bounded gaps that no vendor fills.

It is **not** simpler than "call one API and get an answer" — and no honest
reading of this market supports that option for mixed-content engineering
packages with traceability and residency requirements.

---

## 7. What would change this recommendation

Re-run the bench, do not re-architect, if any of these happen:

- Content Understanding gains intra-page classification — removes C2 entirely;
- Azure-hosted Mistral OCR reaches page and language parity with the first-party
  API — its block classification would then subsume much of the segmentation code;
- Content Understanding Pro mode reaches GA — its multi-document reasoning and
  knowledge-base linking may subsume cross-region reconciliation and enable
  validation against ABB's own catalogue;
- ABB grants a residency waiver — reopens LandingAI ADE and Reducto, which lead
  on spatially complex documents.

---

## 8. Sources

**Microsoft Learn**

- Content Understanding classifier — page-level minimum, splitting, routing
- Content Understanding document overview — `estimateFieldSourceAndConfidence`,
  field extraction methods, normalisation
- Content Understanding analyzer reference — `estimateSourceAndConfidence`,
  nested field configuration
- Content Understanding service limits — 200 MB / 300 pages async, 1,000 fields,
  300 classify categories, supported generative models
- Content Understanding what's new — GA `2025-11-01`; July 2026 grounding for all
  field types; Pro mode
- Content Understanding transparency note — grounding is page number + bounding
  box, document modality only
- Document Intelligence custom classification model — `splitMode` default `none`
  in v4.0 GA
- Foundry Models sold directly by Azure — `mistral-ocr-4-0` Preview and
  `mistral-document-ai-2512`: 30 pages / 30 MB, `en` only
- Retired Foundry Models — Mistral OCR generation churn

**Vendor and community**

- Mistral OCR 4 launch (23 June 2026) — bounding boxes, block classification,
  per-page and per-word confidence, 170 languages, self-host container
- Microsoft Foundry blog — Mistral Document AI with OCR 4 pricing and deployment
- ParseBench (LlamaIndex) — comparative parse quality; vendor-published
- Docling, MinerU, Marker, huridocs/pdf-document-layout-analysis project
  documentation
- LandingAI ADE, Reducto, LlamaParse product documentation on visual grounding
  and per-field citations
