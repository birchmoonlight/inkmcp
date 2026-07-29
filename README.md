# Inkscape MCP Server

A Model Context Protocol (MCP) server that enables live control of Inkscape through natural language instructions. AI assistants (Claude, etc.) can directly manipulate vector graphics in real-time via a native Rust binary — on **Windows, macOS, and Linux**.

## Features

- 🎯 **Universal Element Creation** — Any SVG element with a unified `tag key=value children=[...]` syntax
- ⚡ **Native Rust Binary** — Single ~2.5 MB executable, no runtime dependencies
- 🌍 **Cross-Platform** — CI-compiled for Windows, macOS (Intel + Apple Silicon), Linux (x86_64 + ARM64)
- 🔄 **Dual Transport** — MCP stdio for AI clients + TCP 127.0.0.1:9999 for CLI/Blender
- 🐍 **Python Code Execution** — Run arbitrary inkex code in the live SVG context
- 🖼️ **PNG Export** — Render the canvas via Inkscape CLI with optional base64 return
- 🏗️ **Hierarchical Groups** — Nested elements with automatic ID collision handling
- 🔗 **Blender Integration** — Bidirectional curve transfer between Blender and Inkscape

## Quick Start

### 1. Download

Grab the archive for your platform from the Releases page:

| Platform | Archive |
|----------|---------|
| Linux (x86_64) | `inkmcp-linux-x86_64.zip` |
| Linux (ARM64) | `inkmcp-linux-arm64.zip` |
| macOS (Intel) | `inkmcp-macos-x86_64.zip` |
| macOS (Apple Silicon) | `inkmcp-macos-arm64.zip` |
| Windows (x86_64) | `inkmcp-windows-x86_64.zip` |

Each archive contains the Rust binary (`inkmcpd`/`inkmcpd.exe`) and all required Python scripts.

### 2. Run

```bash
# Extract and start the server (uses inkmcpd binary if found)
./run_inkscape_mcp.sh            # Linux / macOS
run_inkscape_mcp.bat             # Windows
```

The server starts in **daemon mode** — it listens on two channels simultaneously:

- **MCP stdio** — for AI assistant integration
- **TCP 127.0.0.1:9999** — for CLI tools, Blender addon, and direct usage

### 3. Connect with AI Tools

**Claude Code** (`~/.claude-mcp.json` or project `.claude-mcp.json`):

```json
{
  "mcpServers": {
    "inkscape": {
      "command": "path/to/run_inkscape_mcp.sh"
    }
  }
}
```

**Windows**:
```json
{
  "mcpServers": {
    "inkscape": {
      "command": "C:\\path\\to\\run_inkscape_mcp.bat"
    }
  }
}
```

### 4. Verify

Once connected, try:
```
In Inkscape, draw a blue circle with radius 50 at position (100, 100).
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Rust MCP Server (inkmcpd)                     │
│  ┌──────────────────────┐    ┌──────────────────────────────┐   │
│  │   MCP stdio          │    │  TCP 127.0.0.1:9999          │   │
│  │  (AI clients)        │    │  (CLI / Blender / tools)     │   │
│  └───────┬──────────────┘    └─────────┬────────────────────┘   │
│          │                             │                         │
│          └──────────────┬──────────────┘                         │
│                         │                                        │
│                 ┌───────▼────────┐                               │
│                 │  Python Worker  │  JSON-line protocol          │
│                 │ (inkmcp_worker) │  stdin / stdout              │
│                 │  inkex + ops   │                               │
│                 └───────┬────────┘                               │
│                         │                                        │
│                 ┌───────▼────────┐                               │
│                 │  Inkscape CLI  │  export, render               │
│                 └────────────────┘                               │
└─────────────────────────────────────────────────────────────────┘
```

- **Rust Binary** (`inkmcpd`): MCP protocol handler, TCP listener, manages Python worker lifecycle
- **Python Worker** (`inkmcp_worker.py`): Uses `inkex` for all SVG operations
- **Communication**: JSON-line protocol over stdin/stdout between Rust and Python
- **No D-Bus required**: Works on all platforms using TCP / stdin IPC

The Rust binary auto-detects the project's Python virtual environment (`venv/bin/python`) so inkex and its dependencies (numpy, lxml) are available regardless of which system Python is installed.

## Usage Examples

### With AI Assistant
```
"Draw a smooth sine wave"
"Create a logo with a radial gradient and elegant typography"
"Design a data visualization chart with bars and hatch fill"
"Export the current document as high-resolution PNG"
```

### CLI (via TCP)

The server must be running first (or run `run_inkscape_mcp.sh` which starts it in the background):

```bash
# Basic shapes
python inkmcpcli.py circle "cx=100 cy=100 r=50 fill=red"
python inkmcpcli.py rect "x=0 y=0 width=200 height=100 stroke=blue"

# Execute Python/inkex code
python inkmcpcli.py execute-code "code='circle = Circle(); circle.set(\"r\", \"50\"); svg.append(circle)'"

# Document info
python inkmcpcli.py get-info ""

# Export screenshot
python inkmcpcli.py export-document-image "format=png max_size=800"
```

### Python (via TCP transport)

```python
from inkmcp.transport import TcpTransport

transport = TcpTransport()
resp = transport.send_command("rect x=10 y=10 width=200 height=100 fill=blue")
print(resp["data"]["message"])
```

## MCP Tool Reference

**`inkscape_operation`** — Universal tool for all Inkscape operations.

**Input schema:**
- `command` (string, required): Operation string in `tag key=value` format

**Supported commands:**

| Command | Example |
|---------|---------|
| `circle` | `circle cx=100 cy=100 r=50 fill=red` |
| `rect` | `rect x=10 y=10 width=200 height=100 fill=blue` |
| `path` | `path d="M10 10 L100 100" stroke=black` |
| `text` | `text x=50 y=50 font-size=16 content="Hello"` |
| `g` (group) | `g id=group1 children=[{circle cx=50 r=20}, {rect x=0 y=0 w=10 h=10}]` |
| `linearGradient` | `linearGradient x1=0 y1=0 x2=1 y2=1 children=[{stop offset=0 stop-color=red}, {stop offset=1 stop-color=blue}]` |
| `execute-code` | `execute-code code='circle = Circle(); svg.append(circle)'` |
| `get-info` | `get-info` |
| `export-document-image` | `export-document-image format=png return_base64=true` |

## Platform-Specific Notes

### Linux
- Works out of the box
- Install Inkscape via package manager or flatpak

### macOS
- Inkscape CLI at `/Applications/Inkscape.app/Contents/MacOS/inkscape`
- Python worker auto-detects Inkscape's bundled inkex (adds site-packages for numpy etc.)
- Apple Silicon (M1/M2/M3) builds available

### Windows
- Python must be in PATH
- Inkscape CLI at `C:\Program Files\Inkscape\bin\inkscape.exe`
- Use `run_inkscape_mcp.bat` to start

## Hybrid Execution (Blender <-> Inkscape)

Transfer curves between Blender and Inkscape in real-time.

```python
# @local (Blender)
import random
points = [(random.randint(10, 200), random.randint(10, 200)) for _ in range(5)]

# @inkscape
for x, y in points:
    circle = Circle()
    circle.set("cx", str(x))
    circle.set("cy", str(y))
    svg.append(circle)
```

See `BLENDER_HYBRID_README.md` for detailed documentation.

## Development

### Prerequisites

- [Rust](https://rustup.rs/) (1.75+)
- Python 3.10+
- Inkscape (for inkex dependency)

### Local Build

```bash
# Build the Rust binary
cd server
cargo build --release
# Binary at: server/target/release/inkmcpd

# Set up Python virtual environment
cd ..
python3 -m venv inkmcp/venv
source inkmcp/venv/bin/activate
pip install -r inkmcp/requirements.txt
```

### Run from Source

```bash
# The binary auto-detects the venv Python and worker script:
server/target/release/inkmcpd

# Or specify manually:
server/target/release/inkmcpd \
  --python inkmcp/venv/bin/python \
  --worker inkmcp/inkmcp_worker.py
```

### Test the Full Stack

```bash
# Python worker standalone
inkmcp/venv/bin/python -c "
from inkmcp_worker import InkscapeWorker
w = InkscapeWorker()
print(w.handle_command('circle cx=100 cy=100 r=50 fill=red'))
"

# TCP mode (start server, then connect)
server/target/release/inkmcpd --tcp-port 9999 &
echo 'rect x=10 y=10 width=200 height=100 fill=blue' | nc -w 2 127.0.0.1 9999

# MCP mode
echo 'Content-Length: 161\r\n\r\n{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' | \
  server/target/release/inkmcpd --no-tcp
```

### Cross-Compilation (CI)

The project uses GitHub Actions to build for 5 targets. To cross-compile locally:

```bash
# Linux ARM64
rustup target add aarch64-unknown-linux-gnu
cargo build --release --target aarch64-unknown-linux-gnu

# Windows (on Linux/macOS with mingw)
rustup target add x86_64-pc-windows-gnu
cargo build --release --target x86_64-pc-windows-gnu

# macOS ARM64 (build on Apple Silicon)
rustup target add aarch64-apple-darwin
cargo build --release --target aarch64-apple-darwin
```

## Project Structure

| Path | Description |
|------|-------------|
| `server/` | Rust MCP Server (`inkmcpd`) |
| `inkmcp/inkmcp_worker.py` | Python SVG operation worker |
| `inkmcp/inkmcpcli.py` | CLI client (TCP transport) |
| `inkmcp/inkscape_mcp_server.py` | Pure-Python fallback MCP server |
| `inkmcp/transport.py` | TCP transport client library |
| `inkmcp/inkmcpops/` | SVG operation modules (unchanged) |
| `inkscape_mcp.py` | Inkscape extension (unchanged) |
| `run_inkscape_mcp.sh` | macOS / Linux launcher |
| `run_inkscape_mcp.bat` | Windows launcher |
| `.github/workflows/release.yml` | CI build matrix (5 targets) |

## License

[GPL-3.0](LICENSE)
