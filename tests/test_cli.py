from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from goselect_docproc import cli
from goselect_docproc.field_schema import load_field_schema
from goselect_docproc.producers.content_understanding import analyzer_configuration, field_analyzer, router_analyzer


def test_client_uses_explicit_preview_version(monkeypatch):
    factory = Mock()
    monkeypatch.setattr("azure.ai.contentunderstanding.ContentUnderstandingClient", factory)
    monkeypatch.setenv("CONTENTUNDERSTANDING_ENDPOINT", "https://example.test")
    monkeypatch.setenv("CONTENTUNDERSTANDING_API_KEY", "test-key")
    cli._content_understanding_client()
    assert factory.call_args.kwargs["api_version"] == "2026-06-01-preview"


def test_run_directories_are_unique_unless_explicit():
    args = SimpleNamespace(pdf=["schedule.pdf"], out=None)
    assert cli._run_dir(args) != cli._run_dir(args)
    args.out = "chosen-output"
    assert str(cli._run_dir(args)) == "chosen-output"


def test_preflight_checks_full_schema_without_analysis_or_deployment():
    client = Mock()
    router = router_analyzer(field_analyzer_ids={"text": "fields", "schedule": "fields"})
    fields = field_analyzer(load_field_schema())
    client.get_analyzer.side_effect = lambda name: {"router": router, "fields": fields}[name]
    assert set(analyzer_configuration(client, "router")) == {"router", "fields"}
    fields.field_schema.fields["rows"].item_definition.properties.pop("motor__electrical__voltage__value")
    with pytest.raises(ValueError, match="incompatible field schema"):
        analyzer_configuration(client, "router")
    client.begin_analyze_binary.assert_not_called()
    client.begin_create_analyzer.assert_not_called()


@pytest.mark.parametrize("mapped_deployment", ["deployment", "wrong", None])
def test_setup_checks_mapping_before_replacing_analyzers(monkeypatch, tmp_path, mapped_deployment):
    client = Mock()
    client.get_defaults.return_value.model_deployments = {"logical-model": mapped_deployment}
    monkeypatch.setattr(cli, "_content_understanding_client", lambda: client)
    monkeypatch.setenv("REPORT_MODEL", "deployment")
    args = SimpleNamespace(
        out=str(tmp_path), analyzer_id="router", completion_model="logical-model", in_page_segments=True,
    )
    if mapped_deployment != "deployment":
        with pytest.raises(SystemExit, match="resource mapping"):
            cli.cmd_setup_analyzer(args)
        client.begin_create_analyzer.assert_not_called()
    else:
        assert cli.cmd_setup_analyzer(args) == 0
        calls = client.begin_create_analyzer.call_args_list
        assert [call.args[0] for call in calls] == ["routerFields", "router"]
        assert calls[-1].args[1].config.content_categories["text"].analyzer_id == "routerFields"
        assert calls[-1].args[1].config.content_categories["schedule"].analyzer_id == "routerFields"
        assert all(call.args[1].models == {"completion": "logical-model"} for call in calls)