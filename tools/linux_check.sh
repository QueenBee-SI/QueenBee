#!/usr/bin/env bash
# QueenBee on Linux, checked the way a user meets it: install.sh, the test suite, the page served, the window path.
#
#   bash tools/linux_check.sh          # from a checkout; uses a state folder of its own and ports 7895-7898
#
# Tried on Ubuntu 22.04 under WSL (2026-10-18). The window path is expected to fall back to the browser unless GTK's or
# Qt's web engine libraries are installed (docs/install.md).
set -u
cd "$(dirname "$0")/.."
export QUEENBEE_STATE_DIR="${TMPDIR:-/tmp}/queenbee-linux-check"
rm -rf "$QUEENBEE_STATE_DIR"; mkdir -p "$QUEENBEE_STATE_DIR"

echo "=== install.sh"
bash scripts/install.sh --no-shortcuts 2>&1 | tail -8
echo "exit ${PIPESTATUS[0]}"
[ -x .venv/bin/python ] || { echo "no .venv: stop"; exit 1; }

echo "=== tests (pytest: tests/conftest.py stands in for agents that are not installed)"
.venv/bin/python -m pip install --quiet pytest 2>&1 | tail -1
.venv/bin/python -m pytest -q tests 2>&1 | tail -1

start() {   # start <label> <port> <host port> [--no-window]
  label=$1; port=$2; hport=$3; shift 3
  echo "=== the app, $label"
  .venv/bin/python -m queenbee.mother --port "$port" --host-port "$hport" "$@" > "$QUEENBEE_STATE_DIR/$label.out" 2>&1 &
  pid=$!
  sleep 20
  if kill -0 $pid 2>/dev/null; then echo "process: running after 20 s"; else wait $pid; echo "process: EXITED, code $?"; fi
  tok=$(cat "$QUEENBEE_STATE_DIR/mother-$port.token" 2>/dev/null)
  echo "ping: $(curl -s -m 5 -H "X-QueenBee-Token: $tok" "http://127.0.0.1:$port/api/ping" || echo 'no answer')"
  grep -i "could not open\|open this address\|opening in your browser" "$QUEENBEE_STATE_DIR/$label.out" | head -2
  kill $pid 2>/dev/null; sleep 1
  pkill -f "queenbee.host --port $hport" 2>/dev/null
}
start no-window 7895 7896 --no-window
start window 7897 7898
echo "=== done (logs in $QUEENBEE_STATE_DIR)"
