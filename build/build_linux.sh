#!/usr/bin/env bash
# Baut dist/VanguardCommander (eine ausführbare Datei). Auf Debian/Ubuntu vorher einmal:
#   sudo apt install python3-venv libxcb-cursor0 libxkbcommon-x11-0 libegl1
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv-build ] || python3 -m venv .venv-build
source .venv-build/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-build.txt
python -m unittest discover -s tests -v
pyinstaller --noconfirm --clean --workpath .pyi-work build/vanguard_commander.spec
echo "Fertig: $(pwd)/dist/VanguardCommander"
