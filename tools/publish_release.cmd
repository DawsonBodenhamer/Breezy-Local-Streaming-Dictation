@echo off
setlocal
set "BLDS_PYTHON=%LOCALAPPDATA%\breezy_local_streaming_dictation\.venv\Scripts\python.exe"
if not exist "%BLDS_PYTHON%" (
    echo Missing configured Python 3.12 runtime: "%BLDS_PYTHON%" 1>&2
    exit /b 1
)
pushd "%~dp0.."
"%BLDS_PYTHON%" -u "tools\publish_release.py" %*
set "BLDS_EXIT=%ERRORLEVEL%"
popd
exit /b %BLDS_EXIT%
