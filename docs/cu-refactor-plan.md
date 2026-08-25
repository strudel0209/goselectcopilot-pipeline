# CU refactor — decision record and verified service facts

**Status: the refactor described here is complete. See `README.md` for how the
pipeline works now.** This file is kept for two things the README does not carry:
the verified Content Understanding service surface (§"CU SDK" and §"Service
facts"), and the record of which claims turned out to be wrong. Do not "correct"
those back — each was checked against the live service or the OpenAPI spec.

Superseded by the README where the two disagree. Known stale below: the
environment section predates the move off `.venv`, and the step list predates the
per-category sub-analyzers. What actually shipped:

- Steps 0-3 done: five-category taxonomy with PLAN drop, SDK client, native
  `sections` tree, contract lifted into a `ContentFieldSchema`.
- Step 4 done: `allow_in_page_segments` measured, rescues pages 2, 3 and 5 of the
  combined-electrical package.
- Step 5 done: the Document Intelligence path is deleted - 1,289 lines.
- **Beyond the plan:** one shared field analyzer did not work. The 112-field
  contract is accepted by the service but truncates at 17 of 42 rows with every
  `electrical` block empty. Replaced by one sub-analyzer per category (22 fields
  for schedules, 18 for prose), with the contract assembled in Python. 42/42 rows.
- **Beyond the plan:** the pipeline now emits the agreed schema and a human review
  sheet (`deliver.py`). It previously produced only internal shapes.

## Environment (migration DONE, needs container rebuild to take effect)
- conda removed. `environment.yml` deleted. Image now `mcr.microsoft.com/devcontainers/python:1-3.12-bookworm`.
- Packages resolve via Microsoft CFS proxy (public PyPI CDN not routable):
  - pip:  `https://packagefeedproxy.microsoft.io/pypi/simple/`  (set as PIP_INDEX_URL in devcontainer.json containerEnv)
  - npm:  `https://packagefeedproxy.microsoft.io/npm/registry/`
  - Downloads redirect to ms-feed-17.pkgs.visualstudio.com — real artefacts, verified by install.
- `requirements.txt` (runtime, read by pyproject `dynamic`) + `requirements-dev.txt` (pip-only, uses `-r`).
- setup.sh: venv at `.venv`, refuses Python != 3.11/3.12 (pymupdf has NO manylinux wheel for cp313;
  source build in this network takes tens of minutes), installs with `--only-binary=:all:`.
- Verified: 72 wheels resolve for cp312; `pytest -q` = 102 passed.

## CU SDK pinned: azure-ai-contentunderstanding==1.2.0b3
- Defaults to api_version `2026-06-01-preview`. GA line (1.1.0, the only conda-forge build) = 2025-11-01, useless for preview features.
- Verified surface:
  - `ContentAnalyzerConfig`: allow_in_page_segments, workflow, chunking_strategy,
    estimate_field_source_and_confidence, content_categories, enable_segment, segment_per_page,
    omit_content, enable_ocr/layout/formula, enable_figure_analysis/description, return_details,
    table_format, chart_format, annotation_format, allow_input_truncation, locales
  - `ContentAnalyzer`: field_schema, dynamic_field_schema, knowledge_sources, models, processing_location
  - `DocumentContent`: segments, chunks, sections, figures, tables, paragraphs, pages, signatures, annotations, hyperlinks
  - `DocumentContentSegment`: category, confidence, segment_id, source, span, start/end_page_number
  - Client: begin_analyze, begin_analyze_binary, analyze_inline, analyze_binary_inline,
    begin_create_analyzer, get/list/update/delete_analyzer, get_result_file, delete_result,
    update_defaults, begin_copy_analyzer, grant_copy_authorization
  - Also: SemanticChunkingStrategy(kind, max_tokens), LabeledDataKnowledgeSource, UsageDetails, to_llm_input()

## Service facts that drive the design
- **In-page segmentation EXISTS** (2026-06-01-preview). Kills the "minimum unit is one page" premise
  behind `regions.py`. Segment `span` = offset/length into markdown; `source` = `D(page,x1,y1,...)`.
- Classifier adds **layout-based feature extraction** in preview (section markers, table headers,
  figure descriptions) — targets sparse/figure-heavy pages. No config needed.
- **Category description length is NOT capped by the API.** The classifier docs page quotes "120
  chars combined name + description", but neither `stable/2025-11-01` nor `preview/2026-06-01-preview`
  encodes a maxLength on `ContentCategoryDefinition.description`. The only length constraints in
  either spec are analyzerId `^[a-zA-Z0-9._-]{1,64}$` (which also means hyphens ARE legal, contrary
  to the old code comment). Long descriptions are accepted in practice. Treat 120 as unverified doc
  prose, not a gate. General "description properties" cap is 1,024.
- Number of categories: 200/analyzer. Hierarchical classification: 5 layers
  (a category's `analyzer_id` routes it to a sub-analyzer).
- Field schema: **1,000 max fields** — the 107-field ABB contract fits natively; the per-content-type
  schema split + `expand()` in extractors.py exists only because Azure OpenAI strict mode caps at 100.
- `estimate_field_source_and_confidence=True` -> page + bbox + 0-1 confidence per field (extract/classify/generate).
- Agentic: `config.workflow="agentic"`, resolves to `agentic.2026-06-01-preview`, advanced
  contextualization rate, ONE input file per request. Replaces retired pro mode (2025-05-01-preview).
- `content_range` e.g. "1-3,5,9-" restricts pages (the "suppress PLANS" ask). Inline analyze caps at 5 pages.
- `poller.continuation_token()` + `begin_analyze(continuation_token=...)` = durable restart for ACA Jobs.
- Async limits: 200 MB / 300 pages. Results auto-deleted after 24h.
- **CU still returns NO figure image bytes** for documents (figure output = description + chart.js/mermaid,
  11 business-chart types only). `get_result_file` is video keyframes. DI's `getAnalyzeResultFigure`
  crops at ~1480x990. => `render.py` + `tiling.py` STAY. This is the real remaining moat.

## Taxonomy (customer's, adopted)
Five categories, lowercase, one per `ContentType`: text / schedule / drawing / plan / other.
Verified live on 2026-08-24 against goselectRouterV2:
- 98624_1_VFDSchedule.pdf   -> 1 seg, SCHEDULE conf 0.98, coverage 100%
- 98813_2_VFDDrawingsNewmarketNHWTF.pdf -> p1 PLAN conf 0.98, p2-3 DRAWING conf 0.99, coverage 100%
  The old 4-category taxonomy called all three "Drawing". Plan/drawing separation is real.
- `drawing` vs `plan` is the load-bearing split. Both are bordered sheets with a title block.
  drawing = schematic, symbols joined by lines, connectivity, NOT to scale (one-line, single line,
  network, control/ladder). plan = scaled view of physical space with dimensions (site, building,
  layout, routing, grounding, sections, elevations, profiles).
- **PLAN is dropped by client policy, at OBJECT granularity - never a page or a file.** Implemented as
  `DropExtractor` in extractors.py, registered via `DROP_REASONS`. Dropping by merely omitting an
  extractor was rejected: it leaves no trace and is indistinguishable from a routing bug. Plan
  segments are still segmented and coverage-proved, so `unexplained_chars` stays 0 and
  `segments_expected` accounting still completes. Sibling segments in the same file are unaffected.
- `Pipeline._warn_on_collateral_drops` logs when a dropped segment also holds TEXT or SCHEDULE
  regions - the whole-page case a page-level classifier forces. A plan's own body is a figure and
  types as a DRAWING region, so it is NOT counted as collateral. This warning disappears once
  `allow_in_page_segments` is on, which is the concrete payoff for step 4.
- The DI-layout heuristic classifier CANNOT emit PLAN — a scaled layout and a one-line are both
  sparse bordered sheets, so geometry can't separate them. Only the CU classifier can. Another point
  for CU in the bench-off.

## Open defects (measured, not guessed)
- **CU classification is not deterministic and there is no cache.** Two consecutive `segment` runs on
  identical bytes of 98813_2_VFDDrawingsNewmarketNHWTF.pdf returned different segmentation:
  run 1 = PLAN p1 (0.98) + DRAWING p2-3 (0.99); run 2 = one DRAWING p1-3 (0.99). n=2, needs proper
  measurement, but it matters three ways: the scorecard is not reproducible from a single run, every
  run bills again (DI results cache by SHA-256, CU results do not), and the plan-drop guarantee is
  only as stable as the classifier. Consider caching the AnalysisResult by content hash.
- **The roles fallback promotes title-block furniture.** On that same drawing package CU returned no
  `sections` array, so the flat role scan ran and tagged `'5/16/2025 11:00:55 AM'` - a plot timestamp
  with role `title` - as a heading, which then became the segment's section_root. `SELF_TITLING` does
  not help: the rule blocks inheriting a *preceding* clause, and this heading sits at the segment
  start so it reads as the sheet's own title. This is what the `section_heading_f1` gate exists to
  catch; do not hand-patch it, label it.

## Step 3 feasibility — PROVEN against the live service (2026-08-24)
Converted `sample_docs/vfd_motor_schema_v1_0.json` (the agreed contract) into a CU
`ContentFieldSchema` and created a throwaway analyzer `goselectSchemaProbe`:
- **112 named fields, max nesting depth 6 — ACCEPTED, status READY, zero warnings.**
- Round-tripped intact: `vfd_motor_pairs[].vfd.electrical.input_voltage.value` survived as
  `{type: string, method: extract, description: ...}`.
- So the docs' "table field = array of objects of *basic* fields" is not a hard limit.
  `ContentFieldDefinition` is fully recursive in both specs (`items` and `properties` both
  self-reference) with no maxDepth. Probe analyzer deleted afterwards.
- `ContentFieldSchema.definitions` + per-field `$ref` exist - the contract repeats
  `{value, unit, details}` about ten times, so define once and ref it.
- Per-field `estimateSourceAndConfidence` overrides the analyzer-level flag.
- **No union types.** ContentFieldType is one of string/date/time/number/integer/boolean/
  array/object/json. The contract's `value: number|string|null` (e.g. "480/277") must become
  `string`; parse in Python afterwards, which is the existing rule anyway.

## Step 3 (option B: category-routed sub-analyzers) — IN PROGRESS
Done so far:
- `field_schema.py` converts the customer's JSON Schema into a `ContentFieldSchema`. The contract
  file stays the source of truth, so a v1.1 is a file swap. Rules: unions collapse to string
  (CU has no union types, and `480/277` must survive verbatim); every leaf is `extract`, never
  `generate`, because a generated value has no place on a page.
- `field_analyzer(schema)` + `router_analyzer(field_analyzer_id=...)`. Categories in
  `FIELD_ROUTED_CATEGORIES = {text, schedule}` carry `analyzer_id`; drawing is deliberately NOT
  routed (needs native-res tiles) and plan is out of scope.
- `setup-analyzer` deploys the field analyzer FIRST - a category cannot reference an analyzer that
  does not exist yet - then the router. Env: `CU_FIELD_ANALYZER_ID`, default `<router>Fields`.

Verified live against goselectRouterV2 on 98624_1_VFDSchedule.pdf:
- Routed response shape: `contents[0]` = router (markdown + segments, no fields),
  `contents[1]` = sub-analyzer (`analyzer_id=...Fields`, `category='schedule'`, `fields=...`).
  So the producer's `_first_document` still gets segmentation from contents[0]; the field entries
  are additional and must be matched to segments by category/page.
- Full 6-level contract extracted with per-field grounding, e.g.
  `tag='VFD-AHU-1-RA-B' conf=0.517 src=D(1,3.0464,9.7583,...)`,
  `product_name='ABB ACH 580' conf=0.697`, `notes='1,2,3,4' conf=0.864`.
  This IS the provenance block the README says the contract lacks - served, not hand-plumbed.
- **Confidences are low (0.52-0.70 on most fields).** Before this can run straight through, the
  customer has to set a review threshold. At 0.25 (today's default) almost everything passes; at
  0.8 almost everything goes to review. Needs labelled data to calibrate - do not guess it.
- `poller.usage` returns real cost telemetry:
  `{documentPagesStandard: 1, contextualizationTokens: 1000, tokens: {gpt-5.4-mini-input: 54865,
  gpt-5.4-mini-output: 27981}}`. Wire this into ProducerCost instead of the flat per-page estimate.

Still to do for step 3: map routed `fields` onto segments and into `ExtractionPayload`, then delete
the per-type schemas and `expand()` for TEXT/SCHEDULE.

### Step 3 mapping — DONE
`expand_contract()` in extractors.py maps contract-shaped service output onto the domain payload,
duck-typed (value/value_object/value_array/source/confidence/spans) so extractors.py takes no SDK
import. `RoutedFieldExtractor` uses routed fields when present and falls back to the model call
otherwise, which is what keeps DI and CU comparable on the same corpus. `parse_source()` turns
`D(page,x1,y1,...)` into page + polygon; `Evidence` gained an optional `confidence`.

Live on 98624_1_VFDSchedule.pdf: **42 VFDs, 42 motors, 42 pairs, job DONE 1/1, ZERO model calls**,
every record carrying page, polygon and confidence.

Two bugs this run exposed, both fixed:
- `assemble.merge` marked a job FAILED whenever `done == 0`, which made a single-segment package
  whose one segment was held for review look like a total failure despite carrying 42 grounded
  records. FAILED now means nothing non-FAILED reached a terminal state.
- **The agreed contract has no motor tag field.** `tag` identifies the pair. Emitting
  `Pair(vfd_tag=tag, motor_tag=tag)` tripped the "drive and motor share tag" validation, which
  exists to catch a real error class. Pairs from the contract are now one-sided
  (`motor_tag=None`) with a note saying so. **This is a customer decision:** a one-sided pair can
  never match a two-tag answer key, so `pair_f1` cannot be scored on schedule-derived pairs until
  the contract gains a motor tag. Raise it alongside the merge-precedence sign-off.
- Electrical values came back null on that schedule. The mapping is unit-tested against
  `{value, unit, details}` (75 kW -> value=75.0 unit='kW' raw='75'), so this is the source document
  or the extraction, not the mapping. Confirm against a schedule that has rating columns.

## Step 4 — MEASURED 2026-08-25 (eval/inpage_bench.py, out/eval/inpage_bench.json)
Two analyzers identical except `allow_in_page_segments`, neither routing fields, so this measures
boundaries not extraction.

**In-page segmentation earns its place, on one document out of four.**
`98850_1_CombinedElectrical.pdf` (10 pages), stable across 3 runs:
```
page-level TEXT:1-1|DRAWING:2-2|SCHEDULE:3-3|DRAWING:4-4|PLAN:5-5|...|PLAN:10-10   rescued: []
in-page    TEXT:1-1|DRAWING:2-2|SCHEDULE:2-2|DRAWING:3-3|SCHEDULE:3-3|DRAWING:4-4|
           PLAN:5-5|TEXT:5-5|PLAN:6-7|PLAN:8-8|PLAN:9-10                            rescued: [2,3,5]
```
Three pages where a grid or prose block was separated from the sheet sharing it. Page 5 is the one
that matters: that prose would have been **dropped with the plan** at page granularity.

Other documents: `98813` 1 -> 3 segments (splits page 2 into TEXT + DRAWING). `98878_1` (Package A)
and `98878_2` **identical both ways** - in-page found nothing to split. So the README's "15 of 20
pages carry more than one content kind" is NOT reproduced by the classifier on Package A; that claim
is about *layout elements* (regions.py granularity), which is a finer thing than a classifier
segment. The two are complementary, not substitutes.

**My stated pass criterion "collateral drops -> 0" was wrong.** It counted TEXT/SCHEDULE regions
inside a PLAN segment, but a plan sheet always carries dimension notes, so it can never reach zero
and improvement is invisible. Replaced with `rescued_pages`.

### Determinism
- Page-level: 3/3 identical signatures.
- In-page: category assignments and rescued pages identical 3/3; segment COUNT varied 11/11/10.
  The only variance is how adjacent PLAN pages group (`PLAN:8-8|PLAN:9-10` vs `PLAN:8-10`).
- Across analyzer re-creations the same document gave 5 segments then 10 - again the difference was
  only in how consecutive PLAN pages grouped.
- So: **category assignment is stable; grouping of consecutive same-category pages is not.** For
  this pipeline that instability is harmless, because plans are dropped either way. It would not be
  harmless for a category whose segments are extracted.

### What this does and does not justify
- Turn `allow_in_page_segments` ON: it strictly adds separated content and never broke coverage
  (unexplained_chars == 0 in every run of every configuration).
- It does NOT justify deleting span subtraction. The CU producer's `_classify_elements` still types
  regions inside a segment and is what the coverage proof rests on. Step 5's real scope is the
  **DI path** (di_layout, layout, segmentation heuristic, geometry, regions), not the concept.
- Blocked before scoring: `eval/labels/*.json` predate the taxonomy. They have no PLAN class, so a
  correctly-identified plan scores as a misclassification against a DRAWING label. Page
  classification F1 is invalid until the labels are re-cut.

## Refactor plan (module verdicts)
DELETE: segmentation.py (208), regions.py (140), geometry.py (62), models.py (150),
        layout.py + producers/di_layout.py (265), hand-rolled REST client in content_understanding.py (~200),
        extractors.py schemas + expand() (~180)
SHRINK: sections.py 274 -> ~90 (keep boundaries + drawing-never-inherits policy; CU HAS native `sections`,
        current producer ignores it and only scans paragraph roles — that's a bug),
        validate.py 87 -> ~55 (grounding gate -> service confidence)
KEEP:   contracts.py, spans.py, manifest.py (coverage proof), reconcile.py (tag lexicon),
        render.py + tiling.py, assemble.py (merge precedence = customer policy), pipeline.py, producers/base.py
Net: ~1,400-1,600 of ~3,850 src lines removable (~40%).

## Steps
0. ~~Fix contentCategories descriptions to <=120 combined~~ WITHDRAWN, the limit is not real.
   DONE instead: adopted the customer's 5-category taxonomy + PLAN drop.  [NO preview needed]
1. ~~Replace urllib client with ContentUnderstandingClient~~ DONE. `AzureContentUnderstandingClient`
   and `_as_namespace` deleted; producer speaks SDK models directly; tests build real
   `AnalysisResult`. Analyzer is now a typed `ContentAnalyzer` from `router_analyzer()`.
   `begin_create_analyzer(..., allow_replace=True)` replaces server-side - no 409, no delete window.
2. ~~Point section index at CU native `sections` tree~~ DONE. Walker extracted from sections.py into
   producer-neutral `tree_index(sections, paragraphs)`; DI and CU both adapt onto it. CU strategy is
   now `content-understanding-sections` (tree) -> `-sections-flat` (bare root, emit nothing) ->
   `-roles` (flat fallback). Also fixed: PLAN was inheriting prose clauses because the rule said
   `is not DRAWING`; now `not in SELF_TITLING = {DRAWING, PLAN}`.
3. ~~Lift vfd_motor_schema_v1_0.json into a CU field_schema + estimate_field_source_and_confidence~~
   DONE via option B (category-routed sub-analyzers). See "Step 3 mapping" above. Per-type schemas
   and `expand()` are still present because DRAWING still uses them; they only disappear if the
   drawing branch ever stops calling a model directly, which step 6 tests.
4. Bench allow_in_page_segments vs regions.py on Package A via eval/score.py  [preview]  <- NEXT
   Success metric already wired: `Pipeline._warn_on_collateral_drops` count should fall to zero,
   because a schedule sharing a plan sheet stops being dropped with it.
5. ~~Delete segmentation/regions/geometry/layout/di_layout~~ DONE on branch `remove-di-path`.
   Justification was NOT step 4's collateral-drop metric (that metric was wrong: a plan sheet
   always carries dimension notes, so the count never reaches zero). The decisive fact is a
   CAPABILITY gap: the heuristic classifier structurally cannot emit `PLAN`. A scaled layout and
   a one-line diagram are both sparse bordered sheets, so no geometric measurement separates
   them - meaning the DI path could never implement the customer's plan-drop policy. Keeping it
   as a control arm bought nothing.
   Removed: layout.py (171), producers/di_layout.py (94), segmentation.py (211), regions.py (141),
   geometry.py (62), bench.py (225), tests/conftest.py (153, all DI object-model fakes),
   tests/test_regions_sections.py (150), eval/dpi_ladder.py (82). Shrank sections.py 301 -> 173
   and cli.py 384 -> 322. Dropped the `azure-ai-documentintelligence` dependency.
   Net: 1,289 lines deleted; src 4,299/23 modules -> 3,693/18. 137 tests pass.
   NOTE the plan above over-promised: models.py, extractors.py schemas and expand() were NOT
   deleted, because DRAWING still calls a vision model directly - the service returns no figure
   image bytes. Those only go if step 6 succeeds.
   Section-attribution tests were rewritten against producer-neutral `tree_index` in
   tests/test_sections.py rather than deleted, so the drawing-never-inherits rule stays covered.
6. Agentic control arm on Package B drawings vs the tiling path               [preview]

## Gotchas hit
- Analyzer create is NOT an upsert: a bare PUT/create returns 409 ModelExists, and PATCH reaches only
  description and tags. Use `allow_replace=True`. A stale analyzer returns categories the routing
  table lacks and every segment silently downgrades to OTHER - now logged as a warning.
- `cli.main()` built the argparse parser BEFORE `load_dotenv()`, so `default=os.getenv("CU_ANALYZER_ID")`
  was always None and setup-analyzer targeted the wrong analyzer while the producer used the right
  one. dotenv now loads first.
- Docs prose is not the contract. The 120-char category limit came from a docs table and is
  contradicted by both API specs. Check azure-rest-api-specs before asserting a limit.
- setuptools cannot follow `-r` includes in a requirements file for dynamic metadata:
  `optional-dependencies` silently resolved EMPTY. Only `dependencies` is dynamic now.
- `python3` on the OLD (miniconda) image was 3.14 -> pip tried to source-build pymupdf and hung.
- A duplicate `class TestRouterAnalyzerDefinition` shadowed the earlier one and pytest silently
  collected only the later. Reconcile per-file collection counts, not just the total.
