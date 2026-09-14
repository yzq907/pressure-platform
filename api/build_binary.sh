#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
uv run --with pyinstaller==6.19.0 pyinstaller --clean --noconfirm --onefile \
  --name mysterious-api \
  --distpath dist-onefile \
  --specpath build \
  --collect-submodules app \
  --hidden-import app.core.log_rotation \
  --collect-submodules asyncmy \
  --add-data "$PWD/jmx_base:jmx_base" \
  launcher.py
