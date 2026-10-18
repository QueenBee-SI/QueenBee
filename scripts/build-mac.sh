#!/usr/bin/env bash
# Build QueenBee.app (macOS): a bundle you can zip and hand to someone who has no Python.
#
#   bash scripts/build-mac.sh
#
# Result: dist/QueenBee.app (zip it: ditto -c -k --keepParent dist/QueenBee.app dist/QueenBee-macos.zip).
# The person who receives it still needs Claude Code or Codex signed in, and Git for the branch mode; not Python.
# The bundle is not notarized: on first open, right-click the app, choose Open, and confirm. Not yet run on a real Mac here.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || bash scripts/install.sh --no-shortcuts
py=.venv/bin/python
$py -m pip install --quiet pyinstaller
$py -m PyInstaller --noconfirm --clean --windowed --name QueenBee \
  --icon queenbee/assets/queenbee.png \
  --collect-submodules queenbee --collect-data queenbee --collect-all webview \
  --add-data "queenbee/mother/demo_free:queenbee/mother/demo_free" \
  --add-data "queenbee/mother/static:queenbee/mother/static" \
  queenbee/launcher.py
echo
echo "Built dist/QueenBee.app"
