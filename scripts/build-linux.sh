#!/usr/bin/env bash
# Build QueenBee for Linux (x86_64): a folder you can tar and hand to someone who has no Python.
#
#   bash scripts/build-linux.sh
#
# Result: dist/QueenBee/QueenBee (tar it: tar -C dist -czf dist/QueenBee-<version>-linux-x86_64.tar.gz QueenBee).
# The person who receives it still needs Claude Code or Codex signed in, and Git for the branch mode; not Python.
# The window needs GTK's or Qt's web engine libraries on the machine; without them QueenBee opens the page in the
# browser instead (docs/install.md).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || bash scripts/install.sh --no-shortcuts
py=.venv/bin/python
$py -m pip install --quiet pyinstaller
$py -m PyInstaller --noconfirm --clean --name QueenBee \
  --collect-submodules queenbee --collect-data queenbee --collect-all webview \
  --add-data "queenbee/mother/demo_free:queenbee/mother/demo_free" \
  --add-data "queenbee/mother/static:queenbee/mother/static" \
  queenbee/launcher.py
echo
echo "Built dist/QueenBee/QueenBee"
