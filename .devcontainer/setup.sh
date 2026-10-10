#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install --no-cache-dir Flask pillow numpy opencv-python-headless requests tensorflow-cpu gunicorn
.venv/bin/python - <<'PY'
import json, secrets
from pathlib import Path
path = Path('config.json')
if not path.exists():
    path.write_text(json.dumps({'flask': {'debug': False, 'port': 5000, 'secret_key': secrets.token_urlsafe(48)}}), encoding='utf-8')
PY
