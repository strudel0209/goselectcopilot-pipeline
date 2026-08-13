#!/usr/bin/env bash
set -euo pipefail

# conda-forge only. `defaults` is excluded deliberately: it carries Anaconda's
# commercial terms for large organisations.
cat > "${HOME}/.condarc" <<'EOF'
channels:
  - conda-forge
channel_priority: strict
auto_activate_base: false
EOF

cd "$(dirname "$0")/.."

# Dedicated env, so a bad solve can never damage `base`.
if conda env list | grep -qE '^goselect\s'; then
  conda env update -n goselect -f environment.yml
else
  conda env create -f environment.yml
fi

conda clean -afy

echo ""
echo "Environment ready: /opt/conda/envs/goselect/bin/python"
/opt/conda/envs/goselect/bin/python - <<'PY'
import azure.ai.documentintelligence as di
print("azure-ai-documentintelligence", di.__version__ if hasattr(di, "__version__") else "installed")
PY
