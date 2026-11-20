#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --locked ruff check backend tests scripts
uv run --locked ruff format --check backend tests scripts
uv run --locked mypy
uv run --locked pytest -q
npm run contracts
npm run typecheck
npm test
npm run build
npm run package:backend
npm run synth
