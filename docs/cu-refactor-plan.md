# GoSelect docproc — CU refactor plan + verified facts (Aug 2026)

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
1. Replace urllib client with ContentUnderstandingClient  [NO preview needed]  <- NEXT
2. Point section index at CU native `sections` tree       [NO preview needed]
3. Lift vfd_motor_schema_v1_0.json into a CU field_schema + estimate_field_source_and_confidence;
   delete per-type schemas and expand()                   [NO preview needed]
4. Bench allow_in_page_segments vs regions.py on Package A via eval/score.py  [preview]
5. Delete segmentation/regions/geometry/layout/di_layout if 4 holds           [preview]
6. Agentic control arm on Package B drawings vs the tiling path               [preview]

## Gotchas hit
- Docs prose is not the contract. The 120-char category limit came from a docs table and is
  contradicted by both API specs. Check azure-rest-api-specs before asserting a limit.
- setuptools cannot follow `-r` includes in a requirements file for dynamic metadata:
  `optional-dependencies` silently resolved EMPTY. Only `dependencies` is dynamic now.
- `python3` on the OLD (miniconda) image was 3.14 -> pip tried to source-build pymupdf and hung.
- A duplicate `class TestRouterAnalyzerDefinition` shadowed the earlier one and pytest silently
  collected only the later. Reconcile per-file collection counts, not just the total.
