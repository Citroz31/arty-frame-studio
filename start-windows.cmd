@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto dependencies
py -3 --version >nul 2>&1
if errorlevel 1 goto python_fallback
py -3 -m venv .venv
if errorlevel 1 goto failed
goto dependencies

:python_fallback
python --version >nul 2>&1
if errorlevel 1 goto missing_python
python -m venv .venv
if errorlevel 1 goto failed

:dependencies
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto missing_python
if /i "%~1"=="--setup-only" goto install
if exist ".venv\arty-frame-studio.ready" goto launch

:install
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -e .
if errorlevel 1 goto failed
type nul > ".venv\arty-frame-studio.ready"
if /i "%~1"=="--setup-only" exit /b 0

:launch
if /i "%~1"=="--diagnose-jtag" goto diagnose_jtag
if /i "%~1"=="--diagnose-com7" goto diagnose_com7
".venv\Scripts\python.exe" -m arty_frame_studio.app
if errorlevel 1 goto failed
exit /b 0

:diagnose_jtag
".venv\Scripts\python.exe" -m arty_frame_studio.cli jtag-diagnose
set "AFS_DIAGNOSTIC_EXIT=%errorlevel%"
pause
exit /b %AFS_DIAGNOSTIC_EXIT%

:diagnose_com7
".venv\Scripts\python.exe" -m arty_frame_studio.cli diagnose --port COM7 --timeout 2
set "AFS_DIAGNOSTIC_EXIT=%errorlevel%"
pause
exit /b %AFS_DIAGNOSTIC_EXIT%

:missing_python
echo Python 3.11 ou plus recent est necessaire dans votre compte utilisateur.
echo Ce lanceur ne demande ni droits administrateur, ni WSL, ni changement de pilote.
pause
exit /b 1

:failed
echo Le lancement a echoue. Le message d'erreur figure ci-dessus.
pause
exit /b 1
