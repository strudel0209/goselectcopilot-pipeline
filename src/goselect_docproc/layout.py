"""Document Intelligence access.

One Layout call per file, cached by content hash. Layout is billed per page, so
the same bytes are never analysed twice.

Feature policy is deliberate and is the cheapest win in the whole pipeline:

* ``FORMULAS`` is **off**. On engineering drawings the box drawn around a tag is
  detected as a radical sign, turning ``VFD-401`` into ``$\\sqrt{150-401}$``.
* ``OCR_HIGH_RESOLUTION`` is a premium add-on and is enabled **per segment**,
  only on drawings, never globally.
* ``output=["figures"]`` makes the service return cropped figure images, so
  drawing crops need no local PDF rasterising.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MODEL_LAYOUT = "prebuilt-layout"


@dataclass(frozen=True)
class LayoutOptions:
    high_resolution: bool = False
    formulas: bool = False
    style_font: bool = True
    figures: bool = True
    pages: str | None = None

    @property
    def tag(self) -> str:
        bits = [
            "hr" if self.high_resolution else "",
            "fx" if self.formulas else "",
            "sf" if self.style_font else "",
            f"p{self.pages}" if self.pages else "",
        ]
        return "-".join(b for b in bits if b) or "base"


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_path(digest: str, options: LayoutOptions, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{digest[:16]}-layout-{options.tag}.json"


def load_cached(path: Path) -> Any:
    from azure.ai.documentintelligence.models import AnalyzeResult

    return AnalyzeResult(json.loads(path.read_text(encoding="utf-8")))


def save_cached(result: Any, path: Path) -> None:
    path.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")


def _features(options: LayoutOptions) -> list:
    from azure.ai.documentintelligence.models import DocumentAnalysisFeature

    selected = []
    if options.style_font:
        selected.append(DocumentAnalysisFeature.STYLE_FONT)
    if options.high_resolution:
        selected.append(DocumentAnalysisFeature.OCR_HIGH_RESOLUTION)
    if options.formulas:
        selected.append(DocumentAnalysisFeature.FORMULAS)
    return selected


class LayoutClient:
    """Thin, cache-first wrapper. The only place the DI SDK is touched."""

    def __init__(self, client: Any, cache_dir: Path | str = ".cache") -> None:
        self._client = client
        self.cache_dir = Path(cache_dir)
        self._result_ids: dict[str, str] = {}

    def analyze(self, data: bytes, options: LayoutOptions | None = None) -> tuple[Any, str]:
        """Return ``(AnalyzeResult, content_sha256)``, served from cache when possible."""
        from azure.ai.documentintelligence.models import (
            AnalyzeOutputOption,
            DocumentContentFormat,
        )

        options = options or LayoutOptions()
        digest = content_sha256(data)
        path = cache_path(digest, options, self.cache_dir)

        if path.exists():
            log.info("layout cache hit %s", path.name)
            return load_cached(path), digest

        if self._client is None:
            raise RuntimeError(
                f"cache miss for {path.name} and no Document Intelligence client configured; "
                "set DOCUMENTINTELLIGENCE_ENDPOINT or pre-populate the cache"
            )

        kwargs: dict[str, Any] = {
            "body": data,
            "output_content_format": DocumentContentFormat.MARKDOWN,
            "features": _features(options),
        }
        if options.figures:
            kwargs["output"] = [AnalyzeOutputOption.FIGURES]
        if options.pages:
            kwargs["pages"] = options.pages

        poller = self._client.begin_analyze_document(MODEL_LAYOUT, **kwargs)
        result = poller.result()

        operation_id = (poller.details or {}).get("operation_id")
        if operation_id:
            self._result_ids[digest] = operation_id.split("/")[-1]

        save_cached(result, path)
        log.info("layout analysed and cached %s", path.name)
        if options.figures:
            self._persist_figures(result, digest)
        return result, digest

    def _persist_figures(self, result: Any, digest: str) -> None:
        """Fetch crops now, while the result id is certainly still valid.

        The service serves crops from ``/analyzeResults/{resultId}/figures/{id}``
        only while it retains the result, and a cache hit never has a result id at
        all. Deferring the fetch is how drawing segments silently lose their
        images; in production this is the step that writes them to Blob.
        """
        for figure in getattr(result, "figures", None) or []:
            figure_id = getattr(figure, "id", None)
            if not figure_id:
                continue
            try:
                self.figure_png(digest, figure_id)
            except Exception as exc:  # noqa: BLE001 - a missing crop is not a failed analysis
                log.warning("figure %s could not be persisted: %s", figure_id, exc)

    def figure_png(self, digest: str, figure_id: str) -> bytes | None:
        """Server-side cropped figure image.

        Requires ``output=["figures"]`` on the originating call and is only
        available while the analyze result is retained by the service, so crops
        are cached to disk on first fetch.
        """
        cached = self.cache_dir / f"{digest[:16]}-figure-{figure_id}.png"
        if cached.exists():
            return cached.read_bytes()

        result_id = self._result_ids.get(digest)
        if not result_id:
            log.warning("no result id for %s; figure %s unavailable", digest[:8], figure_id)
            return None

        stream = self._client.get_analyze_result_figure(
            model_id=MODEL_LAYOUT, result_id=result_id, figure_id=figure_id
        )
        payload = b"".join(stream)
        cached.write_bytes(payload)
        return payload
