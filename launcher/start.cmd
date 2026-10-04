@echo off
REM ---------------------------------------------------------------------------
REM One-click launcher for the DeepSeek Harness Web UI in this checkout.
REM
REM   start.cmd                    open the interactive menu: start the Web UI,
REM                                manage plugins, look up every command
REM   start.cmd --rebuild          force "pnpm run build" before launching
REM   start.cmd --dev              also run "pnpm run dev:web" (client-plugin HMR
REM                                watcher) in a separate window
REM   start.cmd --launcher-help    print this help
REM   start.cmd <dsh arguments>    run the dsh CLI directly, without the menu
REM
REM Examples:
REM   start.cmd web --port 8080
REM   start.cmd web --no-open
REM   start.cmd headless "summarize this repository"
REM
REM The menu lives in start_menu.py (standard library only; Chinese survives any
REM console code page). Plugin management forwards to pnpm inside
REM $DSH_HOME/profiles/<name>, so it never edits plugin sources. Only the
REM supported dsh application launcher (apps/cli/src/bin.ts) is invoked;
REM see scripts/verify-application-entrypoints.ts.
REM
REM Both files live in launcher/. Double-click start.cmd, or add launcher/ to
REM PATH to call "start.cmd" from any working directory.
REM ---------------------------------------------------------------------------

setlocal
title DeepSeek Harness

set "SCRIPT_DIR=%~dp0"
REM This file and start_menu.py live in launcher/; the repository root is the
REM parent directory, and every relative path below resolves from there.
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"
set "DSH_ENTRY=%REPO_ROOT%\apps\cli\src\bin.ts"
set "FORCE_BUILD=0"
set "START_DEV=0"
set "DSH_ARGS="

:parse_args
if "%~1"=="" goto args_ready
if /I "%~1"=="--launcher-help" goto usage
if /I "%~1"=="--rebuild" goto flag_rebuild
if /I "%~1"=="--dev" goto flag_dev
if "%DSH_ARGS%"=="" set "DSH_CMD=%~1"
set "DSH_ARGS=%DSH_ARGS% "%~1""
shift
goto parse_args

:flag_rebuild
set "FORCE_BUILD=1"
shift
goto parse_args

:flag_dev
set "START_DEV=1"
shift
goto parse_args

:args_ready
REM No arguments: the menu owns the console. Any argument runs the dsh CLI
REM directly, so scripts and shortcuts keep working.
if "%DSH_ARGS%"=="" goto menu

cd /d "%REPO_ROOT%"
if errorlevel 1 goto no_checkout
if not exist "%DSH_ENTRY%" goto no_entry

where node >nul 2>nul
if errorlevel 1 goto no_node
for /f "tokens=1 delims=." %%v in ('node -v') do set "NODE_MAJOR=%%v"
set "NODE_MAJOR=%NODE_MAJOR:v=%"
if not defined NODE_MAJOR goto no_node
if %NODE_MAJOR% LSS 22 goto old_node

set "NEED_PNPM=0"
if not exist "node_modules\.modules.yaml" set "NEED_PNPM=1"
if not exist "apps\web\dist\index.html" set "NEED_PNPM=1"
if "%FORCE_BUILD%"=="1" set "NEED_PNPM=1"
if "%START_DEV%"=="1" set "NEED_PNPM=1"
if "%NEED_PNPM%"=="1" (
  where pnpm >nul 2>nul
  if errorlevel 1 goto no_pnpm
)

if not exist "node_modules\.modules.yaml" (
  echo [start] Installing workspace dependencies: pnpm install
  call pnpm install
  if errorlevel 1 goto install_failed
)

if "%FORCE_BUILD%"=="1" goto do_build
if not exist "apps\web\dist\index.html" goto do_build
goto build_ready

:do_build
echo [start] Building repository artifacts: pnpm run build
echo [start] The first build can take several minutes.
call pnpm run build
if errorlevel 1 goto build_failed
if not exist "apps\web\dist\index.html" goto build_failed

:build_ready
if "%START_DEV%"=="1" (
  echo [start] Starting the client-plugin HMR watcher in a new window: pnpm run dev:web
  start "dsh dev:web" /D "%REPO_ROOT%" cmd /k pnpm run dev:web
)

REM The default-port check applies only to a plain "web" launch that will bind
REM port 3080. Help and config dumps boot nothing, and an explicit --port names
REM the port the user chose.
if /I not "%DSH_CMD%"=="web" goto launch
echo %DSH_ARGS% | findstr /I /C:"--help" /C:"--dump" /C:"--port" >nul
if not errorlevel 1 goto launch
netstat -ano -p TCP | findstr /I /C:"LISTENING" | findstr /C:":3080 " >nul
if errorlevel 1 goto launch
echo [start] Something is already listening on port 3080, so no second server was started.
echo [start] If that is this project's Web UI, open http://127.0.0.1:3080.
echo [start] Otherwise stop that process, or choose another port: start.cmd web --port 8080
goto done

:launch
echo [start] Starting DeepSeek Harness: dsh%DSH_ARGS%
node --import "tsx/esm" "%DSH_ENTRY%" %DSH_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" goto done
echo.
echo [start] dsh exited with code %EXIT_CODE%.
pause
REM Propagate the failure: the menu reports this code instead of a success.
endlocal & exit /b %EXIT_CODE%

:menu
REM Reachable only by goto: never fall into it from the launch path.
REM The menu is start_menu.py. "py -3" first, because the launcher can pick a
REM specific Python 3 install; then a plain python on PATH. A Windows Store
REM alias (under WindowsApps) is not a usable interpreter, so it is rejected.
set "PY_EXE="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY_EXE=py -3"
if defined PY_EXE goto menu_run
where python >nul 2>nul
if errorlevel 1 goto menu_no_python
set "PY_PATH="
for /f "delims=" %%p in ('where python 2^>nul') do if not defined PY_PATH set "PY_PATH=%%p"
echo "%PY_PATH%" | findstr /I /C:"WindowsApps" >nul
if not errorlevel 1 goto menu_no_python
set "PY_EXE=python"

:menu_run
%PY_EXE% "%SCRIPT_DIR%start_menu.py" --repo "%REPO_ROOT%"
set "MENU_EXIT=%ERRORLEVEL%"
if not "%MENU_EXIT%"=="0" (
  echo.
  echo [start] The menu exited with code %MENU_EXIT%.
  pause
)
goto done

:menu_no_python
echo [start] Python was not found on PATH, so the interactive menu cannot start.
echo [start] Install Python 3 from https://www.python.org/downloads/ and tick
echo [start] "Add python.exe to PATH", then run this file again. Without Python
echo [start] every command still works directly, for example:
echo [start]   start.cmd web
echo [start]   start.cmd web --port 8080
pause
goto done

:no_checkout
echo [start] Cannot enter the checkout directory: %REPO_ROOT%
goto fail

:no_entry
echo [start] dsh entry point not found at %DSH_ENTRY%
goto fail

:no_node
echo [start] Node.js was not found on PATH.
echo [start] Install Node.js 22.19+ or 24+ from https://nodejs.org, then run this file again.
goto fail

:old_node
echo [start] Node.js %NODE_MAJOR% is too old; this checkout needs Node.js 22.19+ or 24+.
goto fail

:no_pnpm
echo [start] pnpm was not found on PATH, and it is needed to install or build.
echo [start] Enable it with "corepack enable" or install it with "npm install -g pnpm".
goto fail

:install_failed
echo [start] pnpm install failed.
goto fail

:build_failed
echo [start] pnpm run build failed.
goto fail

:usage
echo One-click launcher for the DeepSeek Harness Web UI from this checkout.
echo.
echo   start.cmd                   open the interactive menu (start / plugins / help)
echo   start.cmd --rebuild         force "pnpm run build" before launching
echo   start.cmd --dev             also run "pnpm run dev:web" (client-plugin HMR)
echo   start.cmd --launcher-help   print this help
echo   start.cmd ^<args^>            run the dsh CLI directly, without the menu
echo.
echo Menu (no arguments):
echo   The menu (start_menu.py) starts the Web UI, manages profile plugins
echo   (add / remove / update / query, including local plugin directories), and
echo   prints the full command reference with examples.
echo.
echo Examples:
echo   start.cmd web --port 8080
echo   start.cmd web --no-open
echo   start.cmd headless "summarize this repository"
echo.
pause
goto done

:fail
echo.
pause
endlocal & exit /b 1

:done
endlocal & exit /b 0
