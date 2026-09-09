import json
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pymupdf
import pytest

from goselect_docproc.document_set import comparison_html, load_document_set, prepare_document_set, run_document_set
from goselect_docproc.field_schema import empty_contract_row
from goselect_docproc.models import AzureOpenAIModel


@pytest.fixture
def saved_run(tmp_path):
    from goselect_docproc.contracts import FileRef, Manifest, Region, Segment, Span
    from uuid import uuid4

    pdf_path = tmp_path / "source with space.pdf"
    with pymupdf.open() as document:
        document.new_page(width=144, height=144)
        document.save(pdf_path)
    content = "drive text drawing secret plan"
    regions = [("TEXT", 0, 10), ("DRAWING", 11, 7), ("PLAN", 19, 11)]
    manifest = Manifest(job_id=str(uuid4()), correlation_id=str(uuid4()), files=[FileRef(
        file_id="f1", ordinal=0, source_uri=pdf_path.as_uri(), page_count=1, content_chars=len(content),
        content_sha256=sha256(pdf_path.read_bytes()).hexdigest(),
    )], segments=[Segment(segment_id=f"s{index}", file_id="f1", first_page=1, last_page=1,
        content_type=kind, confidence=1, source="D(1,0,0,1,0,1,1,0,1)", source_unit="inch",
        regions=[Region(kind=kind, ref=f"r{index}", page=1, spans=[Span(offset=offset, length=length)])])
        for index, (kind, offset, length) in enumerate(regions)])
    (tmp_path / "manifest.json").write_text(manifest.model_dump_json())
    (tmp_path / "configuration.json").write_text(json.dumps({"drawing_dpi": 72, "drawing_model": "model"}))
    (tmp_path / "f1-analysis.json").write_text(json.dumps({"contents": [{"markdown": content,
        "pages": [{"pageNumber": 1, "angle": 0}]}]}))
    return tmp_path


def test_prepare_keeps_siblings_excludes_plans_and_checks_limits(saved_run):
    packet = prepare_document_set(saved_run)
    assert "drive text" in packet["prompt"] and "drawing" in packet["prompt"]
    assert "secret plan" not in packet["prompt"]
    assert packet["permitted_source_characters"] == 17
    assert len(packet["images"]) == 2
    assert packet["excluded"] == [{"segment": "s2", "category": "PLAN"}]
    with pytest.raises(ValueError, match="images exceed"):
        prepare_document_set(saved_run, max_images=1)
    with pytest.raises(ValueError, match="character guard"):
        prepare_document_set(saved_run, max_characters=1)
    (saved_run / "source with space.pdf").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Source hash"):
        prepare_document_set(saved_run)


def test_run_once_save_raw_replay_and_refusal(saved_run, monkeypatch):
    packet = prepare_document_set(saved_run)
    row = empty_contract_row()
    row["tag"] = "drive-A"
    result = {"specification": {"vfd_motor_pairs": [row]}, "evidence": [],
              "unresolved_requirements": [], "review_issues": []}
    response = Mock()
    response.choices = [SimpleNamespace(finish_reason="stop", message=SimpleNamespace(refusal=None, content=json.dumps(result)))]
    response.model_dump.return_value = {"raw": True}
    response.usage.model_dump.return_value = {"total_tokens": 100}
    model = Mock()
    model._client.chat.completions.create.return_value = response
    constructor = Mock(return_value=model)
    constructor._content.side_effect = AzureOpenAIModel._content
    monkeypatch.setattr("goselect_docproc.document_set.AzureOpenAIModel", constructor)
    output = saved_run / "alternative"
    assert run_document_set(packet, output, endpoint="https://example.test", deployment="model") == result
    assert constructor.call_args.kwargs["max_attempts"] == 1
    assert model._client.chat.completions.create.call_count == 1
    request = model._client.chat.completions.create.call_args.kwargs
    parts = request["messages"][0]["content"]
    assert sum(part["type"] == "image_url" for part in parts) == len(packet["images"])
    assert "SOURCE s1:p1" in parts[1]["text"]
    assert load_document_set(output, packet) == result
    assert json.loads((output / "response.json").read_text()) == {"raw": True}
    with pytest.raises(FileExistsError):
        run_document_set(packet, output, endpoint="https://example.test", deployment="model")
    response.choices[0].finish_reason = "length"
    with pytest.raises(RuntimeError, match="Incomplete"):
        run_document_set(packet, saved_run / "truncated", endpoint="https://example.test", deployment="model")
    assert not (saved_run / "truncated" / "deliverable.json").exists()
    packet["sources"]["s0:p1"]["text"] = "unrelated"
    with pytest.raises(ValueError, match="different source"):
        load_document_set(output, packet)


def test_comparison_escapes_source_and_keeps_duplicate_rows():
    row = empty_contract_row()
    row.update(tag="drive-A", notes="<script>bad()</script>")
    result = {"specification": {"vfd_motor_pairs": [row]}, "evidence": [],
              "review_issues": [], "unresolved_requirements": []}
    html = comparison_html({"vfd_motor_pairs": []}, result)
    assert "new row" in html and "&lt;script&gt;" in html and "<script>" not in html
    html = comparison_html({"vfd_motor_pairs": [row, row]}, result)
    assert "Unmatched identity" in html


def test_text_only_preparation_and_plan_only_rejection(saved_run):
    path = saved_run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["segments"] = [segment for segment in manifest["segments"] if segment["content_type"] != "DRAWING"]
    path.write_text(json.dumps(manifest))
    assert prepare_document_set(saved_run)["images"] == []
    manifest["segments"] = [segment for segment in manifest["segments"] if segment["content_type"] == "PLAN"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Empty source set"):
        prepare_document_set(saved_run)