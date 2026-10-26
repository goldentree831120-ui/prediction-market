#!/usr/bin/env bash
# News study (M5) in one command: tests, then the GDELT fetch (one request per market, cached;
# seconds on a re-run), then the event study. Results go to analysis/results/news_*.
#   ./run_news.sh            everything
#   ./run_news.sh --limit 1  fetch one market only (a quick check that GDELT answers)
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "run ./run.sh once first (it creates .venv)"; exit 1; }
.venv/bin/python -m pytest -q tests
.venv/bin/python news_fetch.py "$@"
if [ "$#" -eq 0 ]; then
  .venv/bin/python news_study.py
fi
