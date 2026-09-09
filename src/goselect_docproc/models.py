"""Azure OpenAI structured extraction through the official Python SDK."""

from __future__ import annotations

import json
from base64 import b64encode
from dataclasses import dataclass, field
from typing import Any

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI


@dataclass
class AzureOpenAIModel:
    """Chat completions with strict JSON schema, and optional image input."""

    endpoint: str
    deployment: str
    api_key: str | None = None
    credential: Any | None = None
    max_completion_tokens: int = 8000
    timeout: int = 300
    max_attempts: int = 4
    _client: OpenAI = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.endpoint = self.endpoint.rstrip("/")
        authentication = self.api_key
        if not authentication:
            self.credential = self.credential or DefaultAzureCredential()
            authentication = get_bearer_token_provider(
                self.credential, "https://ai.azure.com/.default"
            )
        self._client = OpenAI(
            base_url=f"{self.endpoint}/openai/v1/",
            api_key=authentication,
            timeout=self.timeout,
            max_retries=max(0, self.max_attempts - 1),
        )

    @property
    def name(self) -> str:
        return self.deployment

    @staticmethod
    def _content(prompt: str, images: list[bytes] | None) -> Any:
        if not images:
            return prompt
        parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + b64encode(image).decode(),
                        "detail": "high",
                    },
                }
            )
        return parts

    def complete_json(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        response = self._client.chat.completions.create(
            model=self.deployment,
            messages=[{"role": "user", "content": self._content(prompt, images)}],
            max_completion_tokens=self.max_completion_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "extraction", "strict": True, "schema": schema},
            },
        )
        choice = response.choices[0]
        message = choice.message
        if choice.finish_reason != "stop" or message.refusal or not message.content:
            raise RuntimeError(
                f"{self.deployment}: finish_reason={choice.finish_reason!r}, "
                f"refusal={message.refusal!r}"
            )
        return json.loads(message.content)

    def close(self) -> None:
        self._client.close()
