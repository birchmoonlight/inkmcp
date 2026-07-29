#!/usr/bin/env python3
"""
Inkmcp Entry Point

Two modes:
  1. Rust MCP Server (preferred): spawns inkmcpd binary
  2. Pure Python fallback: runs inkscape_mcp_server directly
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def try_rust_binary() -> bool:
    """Try to find and execute the Rust MCP Server binary.

    Returns True if the binary was found and executed (process replaces current).
    Returns False if binary not found.
    """
    script_dir = Path(__file__).parent
    exe_name = "inkmcpd.exe" if sys.platform == "win32" else "inkmcpd"

    # Search locations
    search_paths = [
        script_dir.parent / exe_name,       # Project root
        script_dir.parent / "server" / exe_name,  # Build directory
        script_dir / exe_name,               # Same directory
    ]

    # Also check PATH
    if shutil.which(exe_name):
        search_paths.insert(0, Path(shutil.which(exe_name)))

    for path in search_paths:
        if path.exists() and os.access(str(path), os.X_OK):
            print(f"[inkmcp] Found Rust binary: {path}")
            # Pass our arguments through
            os.execv(str(path), [str(path)] + sys.argv[1:])
            return True  # Never reached, os.execv replaces the process

    return False


def main():
    # Try the Rust binary first (fast path)
    if "--python" not in sys.argv and try_rust_binary():
        pass  # Rust binary took over
    else:
        # Pure Python fallback
        print(
            "[inkmcp] Starting Python MCP Server (fallback mode)...\n"
            "[inkmcp] For better performance, install the Rust binary:\n"
            "[inkmcp]   cd server && cargo build --release\n"
        )
        from inkscape_mcp_server import main as server_main
        server_main()


if __name__ == "__main__":
    main()
