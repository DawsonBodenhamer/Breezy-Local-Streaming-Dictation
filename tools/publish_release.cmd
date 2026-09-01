@echo off
setlocal
pushd "%~dp0.."
python.exe -u "tools\publish_release.py" %*
set "BREEZY_EXIT=%ERRORLEVEL%"
popd
if "%BREEZY_EXIT%"=="3" (
    echo The active Python runtime could not be started. 1>&2
)
if "%BREEZY_EXIT%"=="9009" (
    echo The active Python runtime could not be started. 1>&2
)
exit /b %BREEZY_EXIT%
