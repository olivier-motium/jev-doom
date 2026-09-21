#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ] || [ ! -d node_modules/ai ] || [ ! -f assets/freedoom2.wad ]; then
  echo 'Run ./setup.sh first.' >&2
  exit 1
fi
echo "Open http://127.0.0.1:${PORT:-8767} (Ctrl-C stops the server)"
exec .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port "${PORT:-8767}" --log-level warning --timeout-graceful-shutdown 2
