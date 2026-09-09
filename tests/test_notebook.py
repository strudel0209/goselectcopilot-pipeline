"""Run the notebook against native SDK response models without network access."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pymupdf
import pytest
from azure.ai.contentunderstanding.models import (
    AnalysisResult,
    ArrayField,
    BooleanField,
    ContentSpan,
    DocumentContent,
    DocumentContentSegment,
    DocumentPage,
    DocumentParagraph,
    ObjectField,
    StringField,
)

from goselect_docproc.contracts import Status
from goselect_docproc.producers.content_understanding import field_analyzer, router_analyzer
from goselect_docproc.field_schema import empty_contract_row, flat_fields, get_field, load_field_schema


def test_notebook_json_metadata_and_code_are_valid():
    notebook = json.loads((Path(__file__).parents[1] / "run_goselect_docproc.ipynb").read_text())
    assert notebook["nbformat"] == 4
    identities = []
    for index, cell in enumerate(notebook["cells"]):
        assert cell["metadata"]["language"] == ("python" if cell["cell_type"] == "code" else "markdown")
        identities.append(cell["metadata"]["id"])
        if cell["cell_type"] == "code":
            ast.parse("".join(cell["source"]), filename=f"cell-{index + 1}")
    assert len(set(identities)) == len(identities)


@pytest.mark.parametrize("update_analyzers", [False, True])
def test_notebook_runs_offline_and_replays_analysis(monkeypatch, tmp_path, update_analyzers):
    notebook = json.loads((Path(__file__).parents[1] / "run_goselect_docproc.ipynb").read_text())
    cells = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
    cells = [source.replace("RUN_ALTERNATIVE = True", "RUN_ALTERNATIVE = False", 1)
             if source.startswith("RUN_ALTERNATIVE = True") else source for source in cells]
    setup_index = next(index for index, source in enumerate(cells) if "ensure_analyzer" in source)
    analysis_index = next(index for index, source in enumerate(cells) if "pipeline.segment(sources)" in source)
    assert setup_index < analysis_index
    assert "UPDATE_ANALYZERS = False" in cells[0]
    if update_analyzers:
        cells[0] = cells[0].replace("UPDATE_ANALYZERS = False", "UPDATE_ANALYZERS = True")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr("IPython.display.display", lambda *args: None)
    for name, value in {
        "CONTENTUNDERSTANDING_ENDPOINT": "https://example.test",
        "CONTENTUNDERSTANDING_API_KEY": "test-key",
        "CU_ANALYZER_ID": "router",
        "REPORT_MODEL": "text-model",
        "REPORT_DRAWING_MODEL": "drawing-model",
        "AZURE_OPENAI_ENDPOINT": "https://example.test",
        "AZURE_OPENAI_API_KEY": "test-key",
        "DRAWING_DPI": "200",
        "GOSELECT_PDFS": '["sample_docs/schedule.pdf", "sample_docs/drawing.pdf"]',
        "GOSELECT_BASELINE_RUN": "unused-old-baseline",
        "GOSELECT_ALTERNATIVE_RUN": "unused-old-alternative",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", Mock())
    model = Mock(name="model")
    model.name = "test-model"
    values = {path: get_field(empty_contract_row(), path) for path in flat_fields()}
    values.update(tag="drive-X", motor_tag="motor-X", identified_system=True, all_vfds_scope=[])
    model.complete_json.return_value = {"rows": [values]}
    monkeypatch.setattr("goselect_docproc.models.AzureOpenAIModel", Mock(return_value=model))
    responses = []
    (tmp_path / "sample_docs").mkdir()
    for filename, category in (("schedule.pdf", "schedule"), ("drawing.pdf", "drawing")):
        with pymupdf.open() as document:
            document.new_page(width=144, height=144)
            document.save(tmp_path / "sample_docs" / filename)
        span = ContentSpan(offset=0, length=7)
        response = AnalysisResult(contents=[DocumentContent(
            markdown="VFD-101", unit="inch",
            pages=[DocumentPage(page_number=1, spans=[span])],
            paragraphs=[DocumentParagraph(content="VFD-101", span=span)],
            segments=[DocumentContentSegment(
                segment_id="s1", category=category, confidence=1.0,
                start_page_number=1, end_page_number=1, span=span,
                source="D(1,0,0,1,0,1,1,0,1)",
            )],
        )])
        if category == "schedule":
            response.contents.append(DocumentContent(category=category, start_page_number=1, end_page_number=1, fields={
                "rows": ArrayField(value_array=[ObjectField(value_object={
                    "tag": StringField(value_string="drive-X", source="D(1,0,0,1,0,1,1,0,1)"),
                    "motor_tag": StringField(value_string="motor-X", source="D(1,0,0,1,0,1,1,0,1)"),
                    "identified_system": BooleanField(value_boolean=True),
                })]),
            }))
        responses.append(SimpleNamespace(result=lambda response=response: response, usage=None))
    client = Mock()
    definitions = {"router": router_analyzer("text-model", field_analyzer_ids={"text": "fields", "schedule": "fields"}),
                   "fields": field_analyzer(load_field_schema(), "text-model")}
    client.get_analyzer.side_effect = lambda name: definitions[name]
    client.get_defaults.return_value.as_dict.return_value = {"modelDeployments": {"text-model": "text-model"}}
    client.get_defaults.return_value.model_deployments = {"text-model": "text-model"}
    def create_analyzer(name, definition, **kwargs):
        assert kwargs == {"allow_replace": True}
        assert client.begin_analyze_binary.call_count == 0
        definitions[name] = definition
        return SimpleNamespace(result=lambda: definition)
    client.begin_create_analyzer.side_effect = create_analyzer
    client.begin_analyze_binary.side_effect = responses
    monkeypatch.setattr("azure.ai.contentunderstanding.ContentUnderstandingClient", Mock(return_value=client))

    state = {}
    for index, source in enumerate(cells):
        exec(compile(source, f"notebook-cell-{index + 1}", "exec"), state)

    assert state["job"].status is Status.REVIEW
    assert state["job"].segments_failed == 0
    drawing = next(result for result in state["results"] if result.content_type.value == "DRAWING")
    assert drawing.status is Status.REVIEW
    assert drawing.payload.records[0].row["tag"] == "drive-X"
    assert drawing.payload.records[0].evidence["drawing"][0].page == 1
    assert state["pipeline"].config.drawing_dpi == state["configuration"]["drawing_dpi"] == 200
    assert state["deliverable"]["vfd_motor_pairs"][0]["tag"] == "drive-X"
    assert (state["RUN_DIR"] / "review.md").is_file()
    assert (state["RUN_DIR"] / "f2-analysis.json").is_file()
    assert model.complete_json.call_count == 1
    assert any(call.kwargs.get("images") for call in model.complete_json.call_args_list)
    assert client.begin_create_analyzer.call_count == (2 if update_analyzers else 0)
    client.update_defaults.assert_not_called()
    if update_analyzers:
        assert [call.args[0] for call in client.begin_create_analyzer.call_args_list] == ["routerFields", "router"]
        assert definitions["routerFields"].field_schema.as_dict() == load_field_schema().as_dict()
    assert client.begin_analyze_binary.call_count == 2

    assert state["packet"]["images"]
    assert Path(state["packet"]["baseline_run"]) == state["RUN_DIR"]
    assert "BASELINE_RUN" not in state and "ALTERNATIVE_RUN" not in state
    assert state["alternative_result"] is None
    model._client.chat.completions.create.assert_not_called()
    from goselect_docproc.models import AzureOpenAIModel

    alternative = {"specification": state["deliverable"], "evidence": [],
                   "review_issues": ["Source review required"], "unresolved_requirements": []}
    response = Mock()
    response.choices = [SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
        refusal=None, content=json.dumps(alternative)))]
    response.model_dump.return_value = {"choices": [{"message": {"content": json.dumps(alternative)}}]}
    response.usage.model_dump.return_value = {"total_tokens": 123}
    model._client.chat.completions.create.return_value = response
    AzureOpenAIModel._content.return_value = [{"type": "text", "text": "mock labeled image"}]
    monkeypatch.setattr("goselect_docproc.document_set.AzureOpenAIModel", AzureOpenAIModel)
    alternative_call = cells[-2]
    exec(compile(alternative_call.replace("RUN_ALTERNATIVE = False", "RUN_ALTERNATIVE = True", 1),
                 "alternative-enabled-mocked", "exec"), state)
    assert state["RUN_ALTERNATIVE"] is False
    assert state["alternative_result"] == alternative
    assert model._client.chat.completions.create.call_count == 1
    assert (state["alternative_output"] / "response.json").is_file()
    assert state["deliverable"] == json.loads((state["RUN_DIR"] / "deliverable.json").read_text())
    exec(compile(alternative_call, "alternative-disabled-no-new-call", "exec"), state)
    exec(compile(cells[-1], "alternative-visual-comparison", "exec"), state)
    assert state["baseline"] is state["deliverable"]
    assert state["alternative_result"] == alternative
    assert model._client.chat.completions.create.call_count == 1
    assert client.begin_analyze_binary.call_count == 2

    exec(compile(cells[analysis_index], "analyze-replay", "exec"), state)
    assert all(analysis.cache_hit for analysis in state["pipeline"].analyses().values())
    assert client.begin_analyze_binary.call_count == 2