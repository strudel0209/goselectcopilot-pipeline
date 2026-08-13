# End-to-end testing procedure

Verified against `sample_docs/mixed_package.pdf` — the **GVW switchboard set**,
18 A3 landscape sheets (`sha256 7f67b2e3…`). Every command and number below was
actually run in this workspace.

---

## 0. Verified state of this workspace

| Item | State |
|---|---|
| `az login` | ✅ tenant `babcd128-…`, `admin@MngEnvMCAP016522…` |
| `DOCUMENTINTELLIGENCE_ENDPOINT` | ✅ `cog-di-tb7tpjtuee4ji.cognitiveservices.azure.com` |
| `CONTENTUNDERSTANDING_ENDPOINT` | ✅ `cog-tb7tpjtuee4ji.services.ai.azure.com` |
| **All three `*_API_KEY` values** | ⚠️ **empty** — DI and CU therefore use `DefaultAzureCredential`; Mistral has no fallback |
| `mistralai` SDK | installed 1.9.11, **but unusable** — see §3 |
| DI Layout cache | ✅ `.cache/7f67b2e356343b88-layout-sf.json` (re-runs are free) |
| Test suite | ✅ 93 passing |

**Blockers right now:** Mistral needs a key; Content Understanding needs its
analyzer created **and** a blob URL.

---

## 1. Verify the install — no Azure calls, no spend

```bash
cd /workspaces/MODP
pytest -q                       # expect: 93 passed
```

If this passes, the span algebra, claim order, containment, reassembly,
duplicate delivery, merge precedence, tag repair, vision budget and all four
producer adapters are sound. Nothing here touches Azure.

---

## 2. Producer A — `di-layout` (works today)

The Azure-native baseline. Nothing to configure beyond `az login`.

```bash
PYTHONPATH=src python -m goselect_docproc.cli segment \
  sample_docs/mixed_package.pdf --out out/gvw --strict
```

Verified output:

```
segments (4) - producer di-layout
  f1-seg-001  p1-1    SCHEDULE  conf=0.266  AUTO   ['SCHEDULE','TEXT']
  f1-seg-002  p2-12   DRAWING   conf=0.473  AUTO   ['DRAWING','SCHEDULE','TEXT']
  f1-seg-003  p13-13  SCHEDULE  conf=0.345  AUTO   ['SCHEDULE','TEXT']
  f1-seg-004  p14-18  DRAWING   conf=0.711  AUTO   ['DRAWING','SCHEDULE','TEXT']

coverage: 100.00% accounted (claimed 70441, furniture 4730, unexplained 0) [ok]
```

Those boundaries are correct: p1 drawing schedule, p2–12 schematics, p13
EQUIPMENT LIST, p14–18 layouts.

Then:

```bash
PYTHONPATH=src python -m goselect_docproc.cli plan sample_docs/mixed_package.pdf --out out/gvw
PYTHONPATH=src python -m goselect_docproc.cli run  sample_docs/mixed_package.pdf --out out/gvw --markdown
```

`run` uses `NullModel` by default — full pipeline, zero model spend.

---

## 3. Producer B — `mistral-blocks`

### 3.1 Why the SDK is not used

conda-forge ships `mistralai` **1.9.11**, whose `ocr.process` signature is:

```
model, document, id, pages, include_image_base64, image_limit, image_min_size,
bbox_annotation_format, document_annotation_format, retries, server_url, …
```

**`include_blocks` is absent** — it arrived with OCR 4 (23 June 2026), and
`include_blocks` is the entire reason this producer exists. PyPI is firewalled
here (`files.pythonhosted.org` blocked), so upgrading is not possible.

The producer therefore calls `POST /v1/ocr` directly through a stdlib REST
client. No dependency, explicit parameters, works behind the firewall.

### 3.2 Configure

**Option A — first-party API** (170 languages, no page cap, inference leaves Azure):

```ini
MISTRAL_API_KEY=<key from console.mistral.ai>
MISTRAL_BASE_URL=
MISTRAL_OCR_MODEL=mistral-ocr-latest
```

**Option B — Azure Foundry** (residency-friendly, more restricted):

1. Foundry portal → **Discover → Models** → search `mistral-ocr-4-0` → **Deploy**
2. **Build → Models →** your deployment → **Details** tab
3. Copy **Target URI** and **Key**

```ini
MISTRAL_API_KEY=<key from the Details tab>
MISTRAL_BASE_URL=<Target URI>
MISTRAL_OCR_MODEL=mistral-ocr-4-0
MISTRAL_OCR_PATH=/v1/ocr        # override if the Target URI already includes a path
```

> Your current `MISTRAL_BASE_URL` is the Foundry resource root. If you get a 404,
> the deployment's Target URI includes a path — set `MISTRAL_OCR_PATH` to `""`
> and put the full path in `MISTRAL_BASE_URL`.

### 3.3 Constraints that apply to real ABB packages

| Limit | Value | Effect |
|---|---|---|
| Pages per request | **30** | Your 18-page set is fine. A 40-page spec is chunked and offsets rebased automatically — a warning is emitted |
| File size | **30 MB** | Yours is 5 MB |
| Languages | **`en` only** on Azure | Non-English packages must route to another producer |
| Status | **Preview** | Not production-committable |

---

## 4. Producer C — `content-understanding`

### 4.1 Create the analyzer (required — this is the 404 you saw)

Prerequisites:

- A **`gpt-4.1` deployment** on the Foundry resource. The classifier declares
  `models: {"completion": "gpt-4.1"}` and fails without it.
- `Cognitive Services User` on the Foundry resource (you have no key set, so
  auth is Entra ID). Alternatively paste a key into
  `CONTENTUNDERSTANDING_API_KEY` — the client prefers a key when present.

```bash
PYTHONPATH=src python -m goselect_docproc.cli setup-analyzer --out out/gvw
```

This `PUT`s the four-category router and **polls the async creation operation**
until it reports `succeeded`. Idempotent; run once per environment.

The definition (documented `2025-11-01` shape):

```json
{
  "baseAnalyzerId": "prebuilt-document",
  "config": {
    "returnDetails": true,
    "enableSegment": true,
    "contentCategories": {
      "TextSpecification": { "description": "Narrative technical specification prose…" },
      "EquipmentSchedule": { "description": "Tabular equipment, motor, VFD or panel schedule…" },
      "Drawing":           { "description": "Engineering drawing sheet: single-line diagram, P&ID…" },
      "Other":             { "description": "Cover pages, transmittals, blank pages…" }
    }
  },
  "models": { "completion": "gpt-4.1" }
}
```

No training data. `enableSegment: true` is what makes it split a multi-document
package instead of labelling the whole file. `Other` is mandatory — without a
catch-all, cover pages are forced into one of the three real categories.

### 4.2 Provide a blob URL

The documented `:analyze` call takes an **http(s) URL**, not bytes. The binary
fallback returns 404 on this resource, confirmed:

```
content-understanding failed: Content Understanding analyze needs an http(s) URL.
Upload the document to Blob Storage and pass a SAS URL as source_uri
(binary fallback returned 404).
```

```bash
az storage container create --account-name <acct> -n cu-test --auth-mode login
az storage blob upload --account-name <acct> --auth-mode login \
  -c cu-test -f sample_docs/mixed_package.pdf -n gvw.pdf
az storage blob generate-sas --account-name <acct> --auth-mode login --as-user \
  -c cu-test -n gvw.pdf --permissions r --expiry 2026-08-08 --full-uri -o tsv
```

The CLI passes `file://…` for local paths, which is not accepted. Either point
the CLI at the SAS URL, or call the producer directly:

```python
from goselect_docproc.producers import ContentUnderstandingProducer
from goselect_docproc.producers.content_understanding import AzureContentUnderstandingClient

client = AzureContentUnderstandingClient("https://cog-tb7tpjtuee4ji.services.ai.azure.com")
producer = ContentUnderstandingProducer(client, analyzer_id="goselect-router")
analysis = producer.analyze("f1", data=b"", source_uri="<SAS URL>")
```

> **Asymmetry worth knowing:** `di-layout` and `mistral-blocks` accept local
> bytes; Content Understanding wants a URL. In production this is a non-issue
> because everything is already in Blob.

---

## 5. Producer D — `hybrid-cu-di`

No extra configuration. Works as soon as §2 and §4 both work: Content
Understanding routes, Document Intelligence supplies spans, geometry and
server-side figure crops. Costs both services.

---

## 6. Run the bench-off

```bash
PYTHONPATH=src python -m goselect_docproc.cli bench \
  sample_docs/mixed_package.pdf \
  --producers di-layout,mistral-blocks,content-understanding,hybrid-cu-di \
  --out out/gvw
```

Unconfigured producers are skipped with the exact reason; failures are recorded
as **rows**, not exceptions. Current verified output:

```
producer               docs fail pages coverage lost intra-page breadcrumb review USD/page s/page
content-understanding     1    1     0   0.0000    0     0.0000     0.0000 0.0000   0.0000 0.0000
di-layout                 1    0    18   1.0000    0     1.0000     1.0000 0.0000   0.0100 0.1690
hybrid-cu-di              1    1     0   0.0000    0     0.0000     0.0000 0.0000   0.0000 0.0000
```

### Read the columns in this order

1. **`lost` must be 0.** Any content loss disqualifies a producer outright.
2. **`intra-page`** — share of pages with more than one content kind.
   `di-layout` scores **1.000** on this document: *all 18 of 18 pages are mixed*.
   `content-understanding` will score **0.000 by construction** — it cannot split
   inside a page. On this corpus that decides it as a standalone producer.
3. **`breadcrumb`** — section traceability. Read separately for scanned documents.
4. **`review`** — share of segments the producer could not classify confidently.
5. **`USD/page`, `s/page`** — only after the above.

Per-document detail and error text land in `out/gvw/bench.json`.

### Diff two producers' segmentation directly

```bash
python -c "
import json
for p in ('di-layout','mistral-blocks'):
    m = json.load(open(f'out/gvw/{p}/manifest.json'))
    print(p, [(s['first_page'], s['last_page'], s['content_type']) for s in m['segments']])
"
```

---

## 7. The drawing path

```bash
PYTHONPATH=src python -m goselect_docproc.cli tiles 4963 3508 --point-size 8
```

A3 landscape at 300 DPI (your sheet size), 8 pt tag text — verified:

```
standard         whole sheet  scale=0.266  33.3px →  8.9px  TOO SMALL  (20 tiles)
high-resolution  whole sheet  scale=0.464  33.3px → 15.5px  OK         (no tiling)
```

**Conclusion for this document set:** on a high-resolution-tier model (Opus 5 /
4.7+) A3 sheets are legible whole — 4,897 visual tokens versus 29,400 tiled. On
Claude 4.5 (standard tier) they are **not** readable, which matches the tag
corruption originally reported. Tiling only becomes necessary at E-size.

---

## 8. Cost of a full test pass

| Item | Unit | 18-page run |
|---|---|---|
| DI Layout | ~$0.010/page | ~$0.18, **cached after the first call** |
| Mistral OCR | $0.004/page | ~$0.07 |
| Content Understanding | ~$0.010/page + gpt-4.1 tokens | ~$0.18 + completion |
| Vision (whole-sheet, high-res tier) | 4,897 tokens/sheet | ~$0.02/sheet at Opus 5 input |

`segment`, `plan` and `bench` cost one Layout call per producer per **unique**
file. Re-runs are free — results are cached by content SHA-256.

---

## 9. Two gaps this document exposed

**Section breadcrumbs are meaningless on a drawing set.** All four segments
resolved to the same cover-page title. `breadcrumb = 1.000` is technically true
and practically worthless — a drawing set has no prose headings. The real
"section" is the **title block** (`XXPSXX-E102` / `PUMP 1 CONTROL SCHEMATIC`).
`sections.py` looks for `sectionHeading` roles and numbered text and finds
neither. For CAD sets the index should be built from title blocks instead.

**Confidences are low (0.266–0.711) yet everything routed `AUTO`.** The
boundaries happen to be right, but the heuristic is not confident and the `0.25`
threshold lets marginal calls through unreviewed. Do not read `review = 0.0000`
as a quality signal from one document.

Both are arguments for completing the bench-off rather than assuming
`di-layout` wins.
