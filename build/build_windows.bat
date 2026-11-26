@echo off
REM Baut dist\VanguardCommander.exe  (Voraussetzung: Python 3.11 oder neuer von python.org)
setlocal
cd /d "%~dp0\.."
set PY=py -3
where py >nul 2>nul || set PY=python
if not exist .venv-build (
    %PY% -m venv .venv-build || goto :fehler
)
call .venv-build\Scripts\activate.bat || goto :fehler
python -m pip install --upgrade pip || goto :fehler
python -m pip install -r requirements.txt -r requirements-build.txt || goto :fehler
python -m unittest discover -s tests || goto :fehler
pyinstaller --noconfirm --clean --workpath .pyi-work build\vanguard_commander.spec || goto :fehler
echo.
echo Fertig: %CD%\dist\VanguardCommander.exe
exit /b 0
:fehler
echo.
echo BUILD FEHLGESCHLAGEN - bitte die Meldung oben an Claude schicken.
exit /b 1
