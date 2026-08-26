#!/usr/bin/env bash
# Spotify Account Migrator - launcher for macOS and Linux
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 is not installed."
    echo "  macOS:  brew install python"
    echo "  Debian: sudo apt install python3 python3-pip"
    exit 1
fi

echo "[..] Checking dependencies..."
python3 -m pip install --quiet --upgrade --disable-pip-version-check -r requirements.txt
echo "[ok] Dependencies ready"
echo

exec python3 spotify_migrate.py "$@"
