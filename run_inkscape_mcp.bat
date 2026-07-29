@echo off
REM Inkscape MCP Launcher for Windows
REM Starts the Rust MCP Server or falls back to pure Python mode.
REM
REM Usage:
REM   run_inkscape_mcp.bat              - Uses inkmcpd.exe if available
REM   run_inkscape_mcp.bat --python     - Force pure Python mode
REM   run_inkscape_mcp.bat --help       - Show help

setlocal enabledelayedexpansion

REM --- Configuration ---
set "SCRIPT_DIR=%~dp0"
set "INKMCP_DIR=%SCRIPT_DIR%inkmcp"

REM --- Find Python ---
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "PYTHON=python"
) else (
    where python3 >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set "PYTHON=python3"
    ) else (
        echo ERROR: Python not found. Please install Python 3.10+
        pause
        exit /b 1
    )
)

REM --- Parse command line ---
set "FORCE_PYTHON=0"
for %%a in (%*) do (
    if "%%a"=="--python" set "FORCE_PYTHON=1"
    if "%%a"=="--help" goto :help
    if "%%a"=="-h" goto :help
)

REM --- Try Rust binary first ---
if "%FORCE_PYTHON%"=="0" (
    if exist "%SCRIPT_DIR%inkmcpd.exe" (
        echo [inkmcp] Starting Rust MCP Server...
        "%SCRIPT_DIR%inkmcpd.exe" %*
        if %ERRORLEVEL% EQU 0 exit /b 0
        echo [inkmcp] Rust server exited with code %ERRORLEVEL%, falling back to Python...
    ) else (
        echo [inkmcp] inkmcpd.exe not found, using pure Python mode
    )
)

REM --- Python virtual environment ---
if not exist "%INKMCP_DIR%\venv" (
    echo [inkmcp] Creating Python virtual environment...
    "%PYTHON%" -m venv "%INKMCP_DIR%\venv"
    call "%INKMCP_DIR%\venv\Scripts\activate.bat"
    "%PYTHON%" -m pip install -r "%INKMCP_DIR%\requirements.txt"
) else (
    call "%INKMCP_DIR%\venv\Scripts\activate.bat"
)

REM --- Start in pure Python fallback mode ---
echo [inkmcp] Starting Python MCP Server (fallback mode)...
cd /d "%INKMCP_DIR%"
%PYTHON% -m inkscape_mcp_server
exit /b %ERRORLEVEL%

:help
echo Inkscape MCP Server
echo.
echo Usage: %~nx0 [options]
echo.
echo Options:
echo   --python       Force pure Python mode (skip Rust binary)
echo   --no-tcp       Disable TCP listener (Rust binary only)
echo   --tcp-port N   Set TCP port (default: 9999)
echo   --help, -h     Show this help
echo.
echo Environment:
echo   INKMCP_WORKER  Path to inkmcp_worker.py
echo.
echo The Rust binary (inkmcpd.exe) is the recommended way to run.
echo If not found, falls back to pure Python mode.
goto :eof
