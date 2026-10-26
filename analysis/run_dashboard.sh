#!/usr/bin/env bash
# M6 dashboard: open http://localhost:8501 in your browser (Ctrl+C here to stop).
# Reads only local files; makes no network calls of its own.
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "run ./run.sh once first (it creates .venv)"; exit 1; }
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m streamlit run dashboard.py
