#!/usr/bin/env bash
set -euo pipefail

source ~/.bashrc >/dev/null 2>&1

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${AGENT_BRIDGE_PYTHON:-python3}"

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "Agent Bridge requires Python 3.10 or newer; '$python_bin' was not found." >&2
  exit 1
fi

exec "$python_bin" -B "$script_dir/agent_bridge_mcp.py"
