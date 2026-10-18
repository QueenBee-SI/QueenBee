#!/usr/bin/env bash
# QueenBee: one-step install for macOS and Linux.
#
#   bash scripts/install.sh            # makes .venv, installs QueenBee, puts a launcher on your Desktop
#   bash scripts/install.sh --no-shortcuts
#
# Nothing is installed system-wide: everything goes into a .venv folder next to the code. Run it again any time.
# macOS: not yet verified on a real Mac here; the steps follow the Windows installer one for one.
set -euo pipefail
cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1. $2" >&2; exit 1; }; }
need git "Install Git (macOS: xcode-select --install; Linux: your package manager)."
# The newest Python 3.11+ on the PATH: python3 is often older (Ubuntu 22.04 has 3.10) while a newer one sits beside it.
new_enough() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && new_enough "$cand"; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "QueenBee needs Python 3.11 or newer; $(python3 --version 2>/dev/null || echo 'no python3') was found." >&2
  echo "  macOS: brew install python@3.12, or python.org" >&2
  echo "  Ubuntu 22.04 / Debian: without sudo, 'curl -LsSf https://astral.sh/uv/install.sh | sh' then 'uv python install 3.12';" >&2
  echo "    with sudo, 'sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv'" >&2
  echo "Then run this again." >&2
  exit 1
fi
echo "Using $PY ($("$PY" --version 2>&1))"
# a .venv made earlier with an older Python is made again
if [ -x .venv/bin/python ] && ! new_enough .venv/bin/python; then rm -rf .venv; fi
if ! command -v claude >/dev/null 2>&1 && ! command -v codex >/dev/null 2>&1; then
  echo "Note: neither Claude Code ('claude') nor Codex ('codex') was found. QueenBee drives them; the window's"
  echo "      'Agents on this computer' card installs and signs them in, or run: curl -fsSL https://claude.ai/install.sh | bash"
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating the private environment (.venv) ..."
  "$PY" -m venv .venv || { echo "Could not make .venv with $PY (Debian/Ubuntu: sudo apt install ${PY}-venv)." >&2; exit 1; }
fi
echo "Installing QueenBee ..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[window]"

if [ "${1:-}" != "--no-shortcuts" ]; then
  .venv/bin/python -m queenbee.mother --shortcuts >/dev/null && echo "Launcher 'QueenBee' created on your Desktop."
fi

if [ "$(uname -s)" = "Linux" ] && ! .venv/bin/python -c 'import gi' >/dev/null 2>&1 && ! .venv/bin/python -c 'from qtpy import QtWebEngineCore' >/dev/null 2>&1; then
  echo
  echo "Note: no window library for QueenBee's own window was found, so it will open in your browser. For the window:"
  echo "  sudo apt install libnss3 libxkbcommon-x11-0 libxcb-cursor0 && .venv/bin/pip install 'pywebview[qt]'"
fi

echo
echo "Done. Open 'QueenBee' from your Desktop, or run:  .venv/bin/python -m queenbee.mother"
