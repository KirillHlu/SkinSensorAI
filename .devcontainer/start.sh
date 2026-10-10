#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .codespaces
if [ ! -x .venv/bin/gunicorn ]; then
  echo "Run bash .devcontainer/setup.sh first."
  exit 1
fi
if [ -f .codespaces/server.pid ] && kill -0 "$(cat .codespaces/server.pid)" 2>/dev/null; then
  echo "SkinSensorAI is already running."
  exit 0
fi
export OMP_NUM_THREADS=2
export TF_NUM_INTRAOP_THREADS=2
export TF_NUM_INTEROP_THREADS=1
nohup .venv/bin/gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 2 --timeout 180 --pid .codespaces/server.pid app:app > .codespaces/server.log 2>&1 < /dev/null &
echo "SkinSensorAI is starting on port 5000. Logs: .codespaces/server.log"
