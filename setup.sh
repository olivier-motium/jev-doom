#!/bin/sh
set -eu
cd "$(dirname "$0")"
command -v node >/dev/null || { echo 'Install Node.js 22 or newer.'; exit 1; }
node -e 'if(Number(process.versions.node.split(".")[0])<22)process.exit(1)' || { echo 'Node.js 22 or newer is required.'; exit 1; }
"${PYTHON:-python3}" -c 'import sys; assert (3,10) <= sys.version_info < (3,14), "Use Python 3.10–3.13"'
"${PYTHON:-python3}" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci
.venv/bin/python download_assets.py
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; fi
echo 'Setup complete. Run ./run.sh for the free local preview; add one API key to .env for Jev.'
