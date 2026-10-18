@echo off
REM Windows start, later for the desktop
if not exist .venv (
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)
REM Without arguments the interface starts, with "probe" the diagnostic run.
if "%1"=="probe" (
    shift
    python -m lumixremote.probe %*
) else (
    python -m lumixremote.app
)
