@echo off
REM dsh CLI wrapper for the DeepSeek Harness repository.
REM Makes this repo's `dsh` command runnable from any working directory,
REM mirroring how claude.cmd wraps its built CLI entry.

setlocal

REM Directory where this script lives (the harness checkout root).
set "SCRIPT_DIR=%~dp0"
set "DSH_ENTRY=%SCRIPT_DIR%apps\cli\src\bin.ts"

if not exist "%DSH_ENTRY%" (
    echo Error: dsh entry point not found at %DSH_ENTRY%
    exit /b 1
)

REM Run the CLI through node with tsx's ESM loader (matches package.json bin).
node --import "tsx/esm" "%DSH_ENTRY%" %*
