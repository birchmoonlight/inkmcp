#!/usr/bin/env python3
"""
Inkscape MCP Server (Pure-Python Fallback)
Model Context Protocol server for controlling Inkscape via TCP transport.

Connects to the Rust MCP Server (inkmcpd) on 127.0.0.1:9999.
For production use, run `inkmcpd` instead — this server is provided
as a pure-Python alternative for development and testing.
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, Union

from mcp.server.fastmcp import FastMCP, Context
from mcp.types import ImageContent

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("InkscapeMCP")

# Default TCP transport settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9999


class InkscapeConnection:
    """Manages TCP transport connection to the Rust MCP server"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._transport = None

    @property
    def _tcp(self):
        """Lazy import and connect to the TCP transport."""
        if self._transport is None:
            from transport import TcpTransport
            self._transport = TcpTransport(self.host, self.port, connect=True)
        return self._transport

    def close(self):
        if self._transport is not None:
            try:
                self._transport.disconnect()
            except Exception:
                pass
            self._transport = None

    def is_available(self) -> bool:
        """Check if the Rust MCP server is running and reachable."""
        try:
            # Quick health check via TCP ping
            import socket
            s = socket.create_connection((self.host, self.port), timeout=2)
            s.close()
            return True
        except Exception:
            return False

    def execute_operation(self, operation_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute operation via TCP transport to the Rust MCP server.

        Args:
            operation_data: Operation data dict with "tag", "attributes", etc.

        Returns:
            Response dict with "status" and "data" keys.
        """
        try:
            # Reconstruct command string from operation data
            tag = operation_data.get("tag", "")
            attributes = operation_data.get("attributes", {})
            children = operation_data.get("children", [])

            parts = [tag]
            for k, v in attributes.items():
                if k in ("response_file", "children"):
                    continue
                sv = str(v)
                if " " in sv or "'" in sv:
                    parts.append(f'{k}="{sv}"')
                else:
                    parts.append(f"{k}={v}")

            if children:
                parts.append(f"children={self._format_children(children)}")

            command = " ".join(parts)

            # Send via TCP
            response = self._tcp.send_command(command)

            # Normalize response format (add "status" key for backward compat)
            if response.get("success"):
                data = response.get("data", {})
                return {"status": "success", "data": data}
            else:
                data = response.get("data", {})
                error = data.get("error", "Unknown error")
                return {"status": "error", "data": {"error": error}}

        except Exception as e:
            logger.error(f"Operation execution error: {e}")
            return {"status": "error", "data": {"error": str(e)}}

    @staticmethod
    def _format_children(children: list) -> str:
        """Format children list as bracket syntax."""
        items = []
        for c in children:
            tag = c.get("tag", "")
            attrs = c.get("attributes", {})
            sub = c.get("children", [])
            astr = " ".join(
                f'{k}="{v}"' if " " in str(v) else f"{k}={v}"
                for k, v in attrs.items()
            )
            inner = f"children={InkscapeConnection._format_children(sub)}" if sub else ""
            token = f"{{ {tag} {astr} {inner} }}" if (astr or inner) else f"{{ {tag} }}"
            items.append(token)
        return "[" + ", ".join(items) + "]"


# Global connection instance
_inkscape_connection: Optional[InkscapeConnection] = None


def get_inkscape_connection() -> InkscapeConnection:
    """Get or create TCP transport connection to the Rust MCP server"""
    global _inkscape_connection

    if _inkscape_connection is not None:
        return _inkscape_connection

    _inkscape_connection = InkscapeConnection(
        host=DEFAULT_HOST, port=DEFAULT_PORT
    )

    if not _inkscape_connection.is_available():
        raise Exception(
            f"Cannot connect to inkmcpd at {DEFAULT_HOST}:{DEFAULT_PORT}. "
            f"Make sure the Rust MCP Server is running.\n"
            f"  Start it with: inkmcpd\n"
            f"  Or: python -m inkmcp.run_inkscape_mcp"
        )

    return _inkscape_connection


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    logger.info("Inkscape MCP server (pure-Python fallback) starting up")

    conn = None
    try:
        # Test connection on startup
        try:
            conn = get_inkscape_connection()
            logger.info(
                f"Connected to inkmcpd at {DEFAULT_HOST}:{DEFAULT_PORT}"
            )
        except Exception as e:
            logger.warning(f"Could not connect to inkmcpd on startup: {e}")
            logger.warning(
                "Make sure the Rust MCP Server (inkmcpd) is running before using tools"
            )

        yield {}
    finally:
        if conn:
            conn.close()
        logger.info("Inkscape MCP server shut down")


# Create the MCP server
mcp = FastMCP("InkscapeMCP", lifespan=server_lifespan)


def format_response(result: Dict[str, Any]) -> str:
    """Format operation result for clean AI client display"""
    if result.get("status") == "success":
        data = result.get("data", {})
        message = data.get("message", "Operation completed successfully")

        # Add relevant details based on operation type
        details = []

        # Element creation details
        if "id" in data:
            details.append(f"**ID**: `{data['id']}`")
        if "tag" in data:
            details.append(f"**Type**: {data['tag']}")

        # Selection/info details
        if "count" in data:
            details.append(f"**Count**: {data['count']}")
        if "elements" in data:
            elements = data["elements"]
            if elements:
                details.append(f"**Elements**: {len(elements)} items")
                # Show first few elements
                for i, elem in enumerate(elements[:3]):
                    elem_desc = (
                        f"{elem.get('tag', 'unknown')} ({elem.get('id', 'no-id')})"
                    )
                    details.append(f"  {i + 1}. {elem_desc}")
                if len(elements) > 3:
                    details.append(f"  ... and {len(elements) - 3} more")

        # Export details
        if "export_path" in data:
            details.append(f"**File**: {data['export_path']}")
        if "file_size" in data:
            details.append(f"**Size**: {data['file_size']} bytes")

        # Code execution details
        if "execution_successful" in data:
            if data["execution_successful"]:
                details.append("**Execution**: ✅ Success")
            else:
                details.append("**Execution**: ❌ Failed")
        if "elements_created" in data and data["elements_created"]:
            details.append(f"**Created**: {len(data['elements_created'])} elements")

        # ID mapping (requested → actual)
        if "id_mapping" in data and data["id_mapping"]:
            details.append("**Element IDs** (requested → actual):")
            for requested_id, actual_id in data["id_mapping"].items():
                if requested_id == actual_id:
                    details.append(f"  {requested_id} ✓")
                else:
                    details.append(
                        f"  {requested_id} → {actual_id} (collision resolved)"
                    )

        # Warning for missing IDs
        if "generated_ids" in data and data["generated_ids"]:
            details.append("⚠️  **WARNING: Elements created without IDs**")
            details.append(
                "For better scene management, always specify 'id' for elements:"
            )
            for gen_id in data["generated_ids"]:
                # Extract element type from generated ID (e.g., "circle2863" → "circle")
                elem_type = "".join(c for c in gen_id if c.isalpha())
                details.append(f"  {gen_id} (use: {elem_type} id=my_name ...)")
            details.append(
                "This enables later modification with execute-code commands."
            )

        # Build final response with appropriate emoji
        # Check if this is a failed code execution
        is_code_failure = (
            "execution_successful" in data and not data["execution_successful"]
        )

        emoji = "❌" if is_code_failure else "✅"

        if details:
            return f"{emoji} {message}\n\n" + "\n".join(details)
        else:
            return f"{emoji} {message}"

    else:
        error = result.get("data", {}).get("error", "Unknown error")
        return f"❌ {error}"


@mcp.tool()
def inkscape_operation(ctx: Context, command: str) -> Union[str, ImageContent]:
    """
    Execute any Inkscape operation using the extension system.

    CRITICAL SYNTAX RULES - READ CAREFULLY:
    1. Single string parameter with space-separated key=value pairs
    2. Children use special bracket syntax: children=[{tag attr=val attr=val}, {tag attr=val}]
    3. NOT JSON objects - use space-separated attributes inside braces
    4. Use 'svg' variable in execute-code, NOT 'self.svg'

    Parameter: command (str) - Command string following exact syntax below

    ═══ BASIC ELEMENTS ═══
    MANDATORY: Always specify id for every element to enable later modification:
    "rect id=main_rect x=100 y=50 width=200 height=100 fill=blue stroke=black stroke-width=2"
    "circle id=logo_circle cx=150 cy=150 r=75 fill=#ff0000"
    "text id=title_text x=50 y=100 text='Hello World' font-size=16 fill=black"

    ═══ AUTOMATIC ELEMENT PLACEMENT ═══
    The system automatically places elements in the correct SVG sections:
    - Basic elements (rect, circle, text, path, etc.) → placed directly in <svg>
    - Definitions (linearGradient, radialGradient, pattern, filter, inkscape:path-effect, etc.) → automatically placed in <defs>

    Path effects example (use inkscape: namespace for Inkscape-specific elements):
    "inkscape:path-effect id=effect1 effect=powerstroke is_visible=true lpeversion=1.3 scale_width=1 interpolator_type=CentripetalCatmullRom interpolator_beta=0.2 start_linecap_type=zerowidth end_linecap_type=zerowidth offset_points='0.2,0.5 | 1,0.5 | 1.8,0.5' linejoin_type=round miter_limit=4 not_jump=false sort_points=true" → automatically goes to <defs>
    "path id=mypath d='M 20,50 C 20,50 80,20 80,80' inkscape:path-effect=#effect1 inkscape:original-d='M 20,50 C 20,50 80,20 80,80'" → path with effect applied

    Filters example (nested primitives with children syntax):
    "filter id=grunge children=[{feTurbulence baseFrequency=2.5 numOctaves=3 result=noise}, {feColorMatrix in=noise type=saturate values=0}, {feComponentTransfer children=[{feFuncA type=discrete tableValues='0 0 .3 0 0 .7 0 0 1'}]}, {feComposite operator=out in=SourceGraphic in2=noise}]" → automatically goes to <defs>
    "rect id=grunge_rect x=100 y=100 width=100 height=100 fill=blue filter=url(#grunge)" → rectangle with grunge texture

    Patterns example (repeating graphics):
    "pattern id=dots width=20 height=20 patternUnits=userSpaceOnUse children=[{circle cx=10 cy=10 r=5 fill=red}]" → automatically goes to <defs>
    "rect id=patterned_rect x=100 y=100 width=100 height=100 fill=url(#dots)" → rectangle with dot pattern

    IMPORTANT: Create defs elements (gradients, patterns, filters) as SEPARATE commands, not as children of groups:
    ✅ CORRECT: "linearGradient id=grad1 ..." (separate command) → automatically goes to <defs>
    ✅ CORRECT: "rect id=shape fill=url(#grad1)" (separate command) → uses the gradient
    ❌ WRONG: "g children=[{linearGradient ...}, {rect ...}]" → this makes gradient stay inside group (not in defs!)

    ═══ NESTED ELEMENTS (Groups) ═══
    Groups with children - ALWAYS specify id for parent and ALL children:
    "g id=house children=[{rect id=house_body x=100 y=200 width=200 height=150 fill=#F5DEB3}, {path id=house_roof d='M 90,200 L 200,100 L 310,200 Z' fill=#A52A2A}]"

    ═══ CODE EXECUTION ═══
    Execute Python code - use 'svg' variable, not 'self.svg':
    CRITICAL: inkex elements require .set() method with string values, NOT constructor arguments!
    "execute-code code='rect = inkex.Rectangle(); rect.set(\"x\", \"100\"); rect.set(\"y\", \"100\"); rect.set(\"width\", \"100\"); rect.set(\"height\", \"100\"); rect.set(\"fill\", \"blue\"); svg.append(rect)'"
    "execute-code code='circle = inkex.Circle(); circle.set(\"cx\", \"150\"); circle.set(\"cy\", \"100\"); circle.set(\"r\", \"20\"); svg.append(circle)'"

    Single-line code (use semicolons for multiple statements):
    "execute-code code='for i in range(3): circle = inkex.Circle(); circle.set(\"cx\", str(i*50+100)); circle.set(\"cy\", \"100\"); circle.set(\"r\", \"20\"); svg.append(circle)'"

    Multiline code (MUST be properly quoted with single quotes):
    "execute-code code='for i in range(3):\n    circle = inkex.Circle()\n    circle.set(\"cx\", str(i*50+100))\n    circle.set(\"cy\", \"100\")\n    circle.set(\"r\", \"20\")\n    svg.append(circle)'"

    Finding and modifying elements by ID (use get_element_by_id helper):
    "execute-code code='el = get_element_by_id(\"house_body\"); el.set(\"fill\", \"brown\") if el else None'"

    ═══ INFO & EXPORT OPERATIONS ═══
    "get-selection" - Get info about selected objects
    "get-info" - Get document information
    "export-document-image format=png return_base64=true" - Screenshot

    ═══ GRADIENTS ═══
    Use gradientUnits=userSpaceOnUse with absolute coordinates matching your shape:
    "linearGradient id=grad1 x1=50 y1=50 x2=150 y2=50 gradientUnits=userSpaceOnUse children=[{stop offset=0% stop-color=red}, {stop offset=100% stop-color=blue}]"
    "rect id=shape x=50 y=50 width=100 height=100 fill=url(#grad1)"

    "radialGradient id=glow cx=200 cy=200 r=50 gradientUnits=userSpaceOnUse children=[{stop offset=0% stop-color=#fff}, {stop offset=100% stop-color=#f00}]"
    "circle id=glowing_circle cx=200 cy=200 r=50 fill=url(#glow)"

    ═══ ID MANAGEMENT ═══
    ALWAYS specify id for every element - this enables later modification and scene management:
    - Input: "g id=scene children=[{rect id=house x=0 y=0}, {circle id=sun cx=100 cy=50}]"
    - Returns: {"id_mapping": {"scene": "scene", "house": "house", "sun": "sun"}}
    - Collision handling: If "house" exists, creates "house_1" and returns {"house": "house_1"}

    ═══ SEMANTIC ORGANIZATION ═══
    Use hierarchical grouping with descriptive IDs whenever possible:

    Example - Creating a park scene with tree:
    "g id=park_scene children=[{g id=tree1 children=[{rect id=trunk1 x=100 y=200 width=20 height=60 fill=brown}, {circle id=foliage1_1 cx=110 cy=180 r=25 fill=green}, {circle id=foliage1_2 cx=105 cy=175 r=20 fill=darkgreen}]}, {g id=house children=[{rect id=house_body x=200 y=180 width=80 height=60 fill=beige}, {polygon id=house_roof points='195,180 240,150 285,180' fill=red}]}]"

    ID Naming Examples:
    - Scene Group: id=park_scene, id=city_view, id=landscape
    - Object Groups: id=tree1, id=tree2, id=house, id=car1
    - Parts: id=trunk1, id=house_body, id=car1_wheel_left
    - Sub-parts: id=foliage1_1, id=foliage1_2, id=house_window1

    Later Modification Examples (use get_element_by_id helper):
    - Change trunk color: execute-code code="el = get_element_by_id('trunk1'); el.set('fill', 'darkbrown') if el else None"
    - Move entire tree: execute-code code="el = get_element_by_id('tree1'); el.set('transform', 'translate(50,0)') if el else None"

    """
    try:
        connection = get_inkscape_connection()

        # Parse the command string using the same logic as our client
        from inkmcpcli import parse_command_string

        parsed_data = parse_command_string(command)

        logger.info(f"Executing command: {command}")
        logger.debug(f"Parsed data: {parsed_data}")

        result = connection.execute_operation(parsed_data)

        # Handle image export special case
        if (
            parsed_data.get("tag") == "export-document-image"
            and result.get("status") == "success"
            and "base64_data" in result.get("data", {})
        ):
            # Return actual image for viewport screenshot
            base64_data = result["data"]["base64_data"]
            return ImageContent(type="image", data=base64_data, mimeType="image/png")

        # Format and return text response
        return format_response(result)

    except Exception as e:
        logger.error(f"Error in inkscape_operation: {e}")
        return f"❌ Operation failed: {str(e)}"


def main():
    """Run the Inkscape MCP server"""
    logger.info("Starting Inkscape MCP Server...")
    mcp.run()


if __name__ == "__main__":
    main()
