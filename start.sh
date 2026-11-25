#!/usr/bin/env sh
# CHS-DRG 3.0 grouper launcher for macOS / Linux.
#
#   chmod +x start.sh     (once, after copying the folder)
#   ./start.sh            (or: sh start.sh)
#
# All platform-specific work lives in start.py; this file only locates the
# script directory and picks a python interpreter.

set -eu

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "[ERROR] python3 not found." >&2
    echo "        Install Python 3.10+ (macOS: brew install python3; Ubuntu/Debian: sudo apt install python3)." >&2
    exit 1
fi

exec "$PYTHON" start.py "$@"
