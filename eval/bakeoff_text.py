"""Model bake-off on the real Howey specification text.

Answers one question with a number rather than an opinion: does a flagship model
extract ABB's fields better than a mini, on this document class?

Scores two things separately, because they fail independently:

* **value** - is the extracted figure right;
* **clause** - did the model cite the clause the value actually came from,
  which is problem 2 and the thing a quotation reviewer needs.

Truth comes from ``eval/labels/98878_1_HoweyVFDs.json`` - hand-typed from the PDF.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("/workspaces/MODP/.env")
sys.path.insert(0, "/workspaces/MODP/src")

from goselect_docproc.cli import _layout_client  # noqa: E402

ENDPOINT = os.environ["CONTENTUNDERSTANDING_ENDPOINT"].rstrip("/")
API = "2025-04-01-preview"
TOKEN = subprocess.run(
    ["az", "account", "get-access-token", "--resource",
     "https://cognitiveservices.azure.com", "--query", "accessToken", "-o", "tsv"],
    capture_output=True, text=True, check=True,
).stdout.strip()

FIELDS = {
    "manufacturer": ("ABB", "1.03"),
    "product_name": ("ACQ580", "1.03"),
    "input_voltage_v": (460, "2.02"),
    "input_phases": (3, "2.02"),
    "input_frequency_hz": (60, "2.02"),
    "output_frequency_min_hz": (1, "2.02"),
    "output_frequency_max_hz": (60, "2.02"),
    "short_circuit_rating_aic": (65000, "2.02"),
    "ambient_temp_max_c": (40, "2.02"),
    "altitude_max_ft": (3300, "2.02"),
}

VALUE_PROPS = {
    "manufacturer": {"type": ["string", "null"]},
    "product_name": {"type": ["string", "null"]},
    "input_voltage_v": {"type": ["number", "null"]},
    "input_phases": {"type": ["integer", "null"]},
    "input_frequency_hz": {"type": ["number", "null"]},
    "output_frequency_min_hz": {"type": ["number", "null"]},
    "output_frequency_max_hz": {"type": ["number", "null"]},
    "short_circuit_rating_aic": {"type": ["number", "null"]},
    "ambient_temp_max_c": {"type": ["number", "null"]},
    "altitude_max_ft": {"type": ["number", "null"]},
}

SCHEMA = {
    "name": "vfd_requirements",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "values": {
                "type": "object",
                "properties": VALUE_PROPS,
                "required": list(VALUE_PROPS),
                "additionalProperties": False,
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "clause": {"type": ["string", "null"]},
                        "verbatim": {"type": ["string", "null"]},
                    },
                    "required": ["field", "clause", "verbatim"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["values", "citations"],
        "additionalProperties": False,
    },
}

PROMPT = """You are reading one section of an electrical specification for a water treatment plant.

Extract the variable frequency drive requirements into the schema. Rules:
- Use only what the document states. If it is not stated, return null.
- For every non-null value, add a citation giving the clause number it came from
  (for example "2.02") and the verbatim sentence.
- Do not infer values from general engineering knowledge.

DOCUMENT
--------
"""


def extract(deployment: str, text: str) -> tuple[dict, float, dict]:
    body = {
        "messages": [{"role": "user", "content": PROMPT + text}],
        "max_completion_tokens": 8000,
        "response_format": {"type": "json_schema", "json_schema": SCHEMA},
    }
    request = urllib.request.Request(
        f"{ENDPOINT}/openai/deployments/{deployment}/chat/completions?api-version={API}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = json.loads(response.read())
    elapsed = time.perf_counter() - started
    content = payload["choices"][0]["message"]["content"]
    return json.loads(content), elapsed, payload.get("usage", {})


def matches(expected, actual) -> bool:
    if actual is None:
        return False
    if isinstance(expected, str):
        return expected.upper() in str(actual).upper()
    try:
        return abs(float(actual) - float(expected)) < 1e-6
    except (TypeError, ValueError):
        return False


def main() -> None:
    result, _ = _layout_client(Path("/workspaces/MODP/.cache")).analyze(
        Path("/workspaces/MODP/sample_docs/98878_1_HoweyVFDs.pdf").read_bytes()
    )
    spec_text = result.content[:16888]  # the TEXT segment: pages 1-7

    rows = []
    for deployment in sys.argv[1:]:
        try:
            out, elapsed, usage = extract(deployment, spec_text)
        except urllib.error.HTTPError as exc:
            print(f"{deployment:16s} HTTP {exc.code}: {exc.read().decode()[:160]}")
            continue

        values = out.get("values", {})
        cited = {c["field"]: (c.get("clause") or "") for c in out.get("citations", [])}

        value_hits = sum(matches(exp, values.get(f)) for f, (exp, _) in FIELDS.items())
        clause_hits = sum(
            matches(exp, values.get(f)) and clause in cited.get(f, "")
            for f, (exp, clause) in FIELDS.items()
        )
        rows.append((deployment, value_hits, clause_hits, elapsed, usage, values, cited))

        print(f"\n=== {deployment} ===")
        for field, (expected, clause) in FIELDS.items():
            got = values.get(field)
            ok = "OK " if matches(expected, got) else "BAD"
            cite_ok = "cite OK " if clause in cited.get(field, "") else f"cite {cited.get(field) or '-'!r}"
            print(f"  {ok} {field:26s} got={str(got):12s} want={expected!s:8s} {cite_ok}")

    print("\n" + "=" * 78)
    print(f"{'model':16s} {'values':>8s} {'clauses':>8s} {'sec':>7s} {'in':>7s} {'out':>7s}")
    for name, v, c, secs, usage, *_ in rows:
        print(
            f"{name:16s} {v:>4d}/{len(FIELDS):<3d} {c:>4d}/{len(FIELDS):<3d} "
            f"{secs:>7.1f} {usage.get('prompt_tokens', 0):>7d} {usage.get('completion_tokens', 0):>7d}"
        )

    out_path = Path("/workspaces/MODP/out/model_bakeoff.json")
    out_path.write_text(json.dumps(
        [{"model": n, "values": v, "clauses": c, "seconds": round(s, 2),
          "usage": u, "extracted": vals, "citations": cites}
         for n, v, c, s, u, vals, cites in rows], indent=2), encoding="utf-8")
    print(f"\nwritten to {out_path}")


if __name__ == "__main__":
    main()
