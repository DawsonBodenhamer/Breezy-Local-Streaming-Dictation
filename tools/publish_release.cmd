@echo off
setlocal
set "BREEZY_PYTHON=%LOCALAPPDATA%\breezy_dictation\.venv\Scripts\python.exe"
if not exist "%BREEZY_PYTHON%" (
    echo Missing configured Python 3.12 runtime: "%BREEZY_PYTHON%" 1>&2
    exit /b 1
)
pushd "%~dp0.."
"%BREEZY_PYTHON%" -u "tools\publish_release.py" %*
set "BREEZY_EXIT=%ERRORLEVEL%"
popd
exit /b %BREEZY_EXIT%
