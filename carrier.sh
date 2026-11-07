#!/usr/bin/env bash
# Carrier for macOS and Linux — run ./carrier.sh to open the desktop app,
# or ./carrier.sh <command> for the terminal (e.g. ./carrier.sh debrief).
#
# First run creates a private Python environment in .venv (about a minute).
# Linux: the window needs GTK WebKit, e.g.
#   sudo apt install python3-gi gir1.2-webkit2-4.1      (Debian/Ubuntu/Kubuntu)
# or use Qt instead:  .venv/bin/pip install "pywebview[qt]"
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "Carrier needs Python 3.11 or newer (found: $("$PY" --version 2>&1))." >&2
  exit 1
fi
# .venv/.carrier-installed is written only after a successful install, so a failed
# first run is retried instead of leaving a half-installed app.
if [ ! -f "$HERE/.venv/.carrier-installed" ]; then
  echo "Setting up Carrier — this happens only once…"
  # --system-site-packages lets the venv see the distro's GTK bindings (python3-gi).
  [ -x "$HERE/.venv/bin/python" ] || "$PY" -m venv --system-site-packages "$HERE/.venv"
  "$HERE/.venv/bin/python" -m pip install --quiet --upgrade pip
  "$HERE/.venv/bin/python" -m pip install --quiet -e "$HERE"
  touch "$HERE/.venv/.carrier-installed"
fi
if [ "$#" -eq 0 ]; then
  exec "$HERE/.venv/bin/python" -m carrier.desktop
fi
exec "$HERE/.venv/bin/python" -m carrier "$@"
