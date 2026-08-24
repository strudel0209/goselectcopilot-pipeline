#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Belt and braces: containerEnv covers interactive shells, this covers anything
# that starts before it, and any pip run outside the devcontainer.
PROXY="https://packagefeedproxy.microsoft.io"
mkdir -p "${HOME}/.config/pip"
cat > "${HOME}/.config/pip/pip.conf" <<EOF
[global]
index-url = ${PROXY}/pypi/simple/
disable-pip-version-check = true
EOF
npm config set registry "${PROXY}/npm/registry/" 2>/dev/null || true

PY=$(command -v python3.12 || command -v python3)
# Wheel coverage, not syntax, is the constraint: pymupdf has no manylinux wheel
# for 3.13, and a source build inside this network takes tens of minutes.
"$PY" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 12) else 1)' || {
  echo "ERROR: $("$PY" -V) is unsupported; use Python 3.11 or 3.12." >&2
  exit 1
}

# Project-local venv, so a bad install is one "rm -rf .venv" away from fixed.
"$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
# Everything resolves to a wheel; a source build here means something is wrong,
# and failing in seconds beats discovering it after a long compile.
.venv/bin/python -m pip install --only-binary=:all: -r requirements-dev.txt
.venv/bin/python -m pip install -e . --no-deps

echo ""
echo "Environment ready: $(pwd)/.venv/bin/python"
.venv/bin/python - <<'PY'
from importlib.metadata import version

from azure.ai.contentunderstanding._configuration import (
    ContentUnderstandingClientConfiguration,
)

for package in ("azure-ai-contentunderstanding", "azure-ai-documentintelligence"):
    print(f"{package:34} {version(package)}")

config = ContentUnderstandingClientConfiguration(endpoint="https://example", credential=object())
print(f"{'content understanding api-version':34} {config.api_version}")
PY

