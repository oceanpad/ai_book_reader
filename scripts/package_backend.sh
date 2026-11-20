#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .build
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file .build/requirements.txt > /dev/null
# Python 3.12 Lambda uses AL2023; Pillow needs newer wheels than manylinux2014.
uv pip sync --python-version 3.12 --python-platform aarch64-manylinux_2_28 \
  --only-binary :all: --target .build/lambda .build/requirements.txt
mkdir -p .build/lambda/backend
cp backend/*.py .build/lambda/backend/
