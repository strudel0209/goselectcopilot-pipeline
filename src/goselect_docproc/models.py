"""Azure OpenAI model client.

One implementation of ``ModelClient`` per Foundry deployment. Deliberately built
on the standard library rather than an SDK: the container this runs in has no
route to PyPI, and the REST surface for chat completions is small enough that a
dependency buys nothing.

Auth is **Entra by default**. A key works, but the production recommendation is
managed identity, so the default path is the one that gets tested.

Structured output uses ``response_format: json_schema`` with ``strict: true``.
Verified limits on Azure: **100 object properties, five levels of nesting** - the
reason extraction schemas are split per content type instead of sending ABB's
107-property contract straight to the model.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from base64 import b64encode
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_API_VERSION = "2025-04-01-preview"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


@dataclass
class AzureOpenAIModel:
    """Chat completions with strict JSON schema, and optional image input."""

    endpoint: str
    deployment: str
    api_version: str = DEFAULT_API_VERSION
    api_key: str | None = None
    credential: Any | None = None
    max_completion_tokens: int = 8000
    timeout: int = 300
    max_attempts: int = 4

    _token: str | None = field(default=None, init=False, repr=False)
    _token_expires: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.endpoint = self.endpoint.rstrip("/")
        if not self.api_key and self.credential is None:
            from azure.identity import DefaultAzureCredential

            self.credential = DefaultAzureCredential()

    @property
    def name(self) -> str:
        return self.deployment

    @property
    def url(self) -> str:
        return (
            f"{self.endpoint}/openai/deployments/{self.deployment}"
            f"/chat/completions?api-version={self.api_version}"
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
            return headers
        # Refresh a minute early; a token that expires mid-retry is a confusing 401.
        if not self._token or time.time() > self._token_expires - 60:
            token = self.credential.get_token(SCOPE)
            self._token = token.token
            self._token_expires = float(token.expires_on)
        headers["Authorization"] = f"Bearer {self._token}"
        return headers

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
        body = {
            "messages": [{"role": "user", "content": self._content(prompt, images)}],
            "max_completion_tokens": self.max_completion_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "strict": True, "schema": schema},
            },
        }
        payload = self._post(body)
        self.last_usage = payload.get("usage", {})
        message = payload["choices"][0]["message"]
        text = message.get("content")
        if not text:
            # A refusal or a length stop is a failure to record, never an empty result.
            raise RuntimeError(
                f"{self.deployment} returned no content "
                f"(finish_reason={payload['choices'][0].get('finish_reason')!r}, "
                f"refusal={message.get('refusal')!r})"
            )
        return json.loads(text)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(
                self.url, data=json.dumps(body).encode(), headers=self._headers()
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode()[:300]
                last = RuntimeError(f"HTTP {exc.code}: {detail}")
                if exc.code not in RETRY_STATUS or attempt == self.max_attempts:
                    raise last from exc
                delay = float(exc.headers.get("retry-after") or 2**attempt)
                log.warning("%s attempt %d: HTTP %d, retrying in %.0fs",
                            self.deployment, attempt, exc.code, delay)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                if attempt == self.max_attempts:
                    raise
                time.sleep(2**attempt)
        raise last or RuntimeError("unreachable")
