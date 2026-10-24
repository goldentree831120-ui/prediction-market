#!/usr/bin/env bash
# Calibration study in one command: sets up a local virtual environment (analysis/.venv) on first
# use, runs the tests, then the analysis on pipeline/data/clean. Results go to analysis/results/.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || { echo "python3-venv missing: sudo apt install -y python3-venv"; exit 1; }
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
.venv/bin/python -m pytest -q tests
.venv/bin/python calibration.py "$@"
