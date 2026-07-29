#!/bin/bash
# Inkscape MCP Server Launcher (macOS / Linux)
#
# Starts the Rust MCP Server (inkmcpd) if available, otherwise falls
# back to the pure-Python MCP server.
#
# Usage:
#   ./run_inkscape_mcp.sh              - Uses inkmcpd binary if available
#   ./run_inkscape_mcp.sh --python     - Force pure Python mode
#   ./run_inkscape_mcp.sh --help       - Show help

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INKMCP_DIR="$SCRIPT_DIR"

FORCE_PYTHON=0
EXTRA_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --python) FORCE_PYTHON=1 ;;
        --help|-h)
            echo "Inkscape MCP Server"
            echo ""
            echo "Usage: $(basename "$0") [options]"
            echo ""
            echo "Options:"
            echo "  --python       Force pure Python mode (skip Rust binary)"
            echo "  --no-tcp       Disable TCP listener (Rust binary only)"
            echo "  --tcp-port N   Set TCP port (default: 9999)"
            echo "  --help, -h     Show this help"
            echo ""
            echo "Environment:"
            echo "  INKMCP_WORKER  Path to inkmcp_worker.py"
            echo ""
            echo "The Rust binary (inkmcpd) is the recommended way to run."
            echo "If not found, falls back to pure Python mode."
            exit 0
            ;;
        *) EXTRA_ARGS+=("$arg") ;;
    esac
done

# --- Try Rust binary first ---
if [ "$FORCE_PYTHON" -eq 0 ]; then
    # Look for inkmcpd in script directory, parent, or PATH
    INKMCPD=""
    for candidate in "$SCRIPT_DIR/inkmcpd" "$INKMCP_DIR/inkmcpd" "$(which inkmcpd 2>/dev/null || true)"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            INKMCPD="$candidate"
            break
        fi
    done

    if [ -n "$INKMCPD" ]; then
        echo "[inkmcp] Starting Rust MCP Server: $INKMCPD"

        # If a venv exists, use its Python interpreter so the worker
        # can find inkex and its dependencies (numpy, etc.) regardless
        # of which system Python is installed.
        PY_PREFIX=""
        if [ -d "$INKMCP_DIR/venv" ]; then
            PY_PREFIX="--python $INKMCP_DIR/venv/bin/python"
        fi

        exec "$INKMCPD" $PY_PREFIX "${EXTRA_ARGS[@]}"
    else
        echo "[inkmcp] inkmcpd binary not found, using pure Python mode" >&2
    fi
fi

# --- Python virtual environment ---
if [ ! -d "$INKMCP_DIR/venv" ]; then
    echo "[inkmcp] Creating Python virtual environment..."
    python3 -m venv "$INKMCP_DIR/venv"
    source "$INKMCP_DIR/venv/bin/activate"
    pip install -r "$INKMCP_DIR/requirements.txt"
else
    source "$INKMCP_DIR/venv/bin/activate"
fi

# --- Start in pure Python fallback mode ---
echo "[inkmcp] Starting Python MCP Server (fallback mode)..."
exec python -m inkscape_mcp_server
