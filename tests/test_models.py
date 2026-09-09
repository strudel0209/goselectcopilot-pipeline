import json

import httpx
import pytest
from openai import OpenAI

from goselect_docproc.models import AzureOpenAIModel


@pytest.mark.parametrize("finish_reason,refusal", [("stop", None), ("length", None), ("stop", "declined")])
def test_sdk_request_and_incomplete_responses(monkeypatch, finish_reason, refusal):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "drawing",
            "choices": [{"index": 0, "finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": '{"items": []}', "refusal": refusal}}],
        })

    monkeypatch.setattr(
        "goselect_docproc.models.OpenAI",
        lambda **kwargs: OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond))),
    )
    model = AzureOpenAIModel(endpoint="https://example.test/", deployment="drawing", api_key="test")
    try:
        if finish_reason == "stop" and not refusal:
            assert model.complete_json(prompt="extract", schema={"type": "object"}, images=[b"png"]) == {"items": []}
        else:
            with pytest.raises(RuntimeError):
                model.complete_json(prompt="extract", schema={"type": "object"})
        assert str(requests[0].url) == "https://example.test/openai/v1/chat/completions"
        body = json.loads(requests[0].content)
        assert body["model"] == "drawing"
        assert body["response_format"]["json_schema"]["strict"] is True
    finally:
        model.close()