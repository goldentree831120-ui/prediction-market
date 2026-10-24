#!/usr/bin/env bash
# Rebuild the dataset with one command. Read-only public API, no key, about 4 requests a second.
#   ./run.sh            reuse cached responses where present (a rerun is fast and identical)
#   ./run.sh --clean    delete cached and derived data first: a full rebuild from scratch
# Extra arguments go to pm-fetch (see ./run.sh --help).
set -euo pipefail
cd "$(dirname "$0")"
if [ "${1:-}" = "--clean" ]; then
  shift
  rm -rf data/raw data/clean
fi
cargo run --release --quiet -- "$@"
