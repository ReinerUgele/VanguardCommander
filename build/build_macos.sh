#!/usr/bin/env bash
# Baut dist/VanguardCommander.app auf diesem Mac (Intel oder Apple Silicon) und packt sie als ZIP.
# Aufruf im Projektordner:  bash build/build_macos.sh
set -euo pipefail
cd "$(dirname "$0")/.."

OSV=$(sw_vers -productVersion)
MAJOR=${OSV%%.*}
MINOR=$(echo "$OSV" | cut -d. -f2)
ARCH=$(uname -m)
echo "== macOS $OSV auf $ARCH =="

# Qt-Version passend zum macOS wählen (neuere Qt-Versionen laufen nicht auf alten Systemen)
QT_PIN=()
if [ "$MAJOR" -ge 12 ]; then
    echo "Qt: aktuelle Version"
elif [ "$MAJOR" -eq 11 ]; then
    QT_PIN=("PyQt6<6.8" "PyQt6-Qt6<6.8"); echo "Qt: 6.7 (für macOS 11)"
elif [ "$MAJOR" -eq 10 ] && [ "$MINOR" -ge 14 ]; then
    QT_PIN=("PyQt6<6.5" "PyQt6-Qt6<6.5"); echo "Qt: 6.4 (für macOS 10.14/10.15)"
else
    echo "FEHLER: macOS $OSV ist zu alt – mindestens macOS 10.14 (Mojave) wird gebraucht."; exit 1
fi

# Python 3.10+ suchen (das vorinstallierte /usr/bin/python3 ist oft 3.9 und reicht nicht)
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        PY=$(command -v "$cand"); break
    fi
done
if [ -z "$PY" ]; then
    echo "FEHLER: Python 3.10 oder neuer fehlt."
    echo "Bitte installieren: https://www.python.org/downloads/macos/  (\"macOS 64-bit universal2 installer\")"
    exit 1
fi
echo "Python: $("$PY" --version) ($PY)"

[ -d .venv-build ] || "$PY" -m venv .venv-build
source .venv-build/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-build.txt ${QT_PIN[@]+"${QT_PIN[@]}"}
python -m unittest discover -s tests
pyinstaller --noconfirm --clean --workpath .pyi-work build/vanguard_commander.spec

VERSION=$(python -c "import vanguard_commander as v; print(v.VERSION)")
ZIP="dist/VanguardCommander-${VERSION}-macOS-${ARCH}.zip"
rm -f "$ZIP"
ditto -c -k --keepParent dist/VanguardCommander.app "$ZIP"
echo
echo "== Fertig =="
echo "App:  $(pwd)/dist/VanguardCommander.app"
echo "ZIP:  $(pwd)/$ZIP   <- diese Datei zurückschicken"
open dist 2>/dev/null || true
