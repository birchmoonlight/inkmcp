#!/usr/bin/env python3
"""
Inkmcp Python Worker Process

Stdin/stdout JSON-line worker spawned by the Rust MCP server (inkmcpd).
Maintains an in-memory SVG document using inkex and delegates all
SVG operations to the existing inkmcpops/* modules.

Protocol:
  - Reads one JSON object per line from stdin
  - Writes one JSON object per line to stdout
  - Request:   {"id": N, "command": "..."}
  - Response:  {"id": N, "success": bool, "data": {...}}

Supports the same commands as the original Inkscape extension:
  rect, circle, path, text, g, ..., execute-code, get-info, export, etc.
"""

import glob
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import traceback
from typing import Any, Dict, List, Optional

# Ensure the parent directory is on the path so local imports work
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# ── Auto-detect inkex ──────────────────────────────────────────────────
#
# Strategy:
#   1. Try plain `import inkex` (works when pip-installed or in a venv).
#   2. If that fails, try Inkscape's bundled inkex — first with just the
#      extensions path, then also with Inkscape's own site-packages (for
#      numpy etc.).
#   3. If the bundled numpy C extensions fail due to Python version mismatch
#      (e.g. system Python 3.14 vs. Inkscape Python 3.10), remove the
#      site-packages path and try one more time.

def _find_and_import_inkex():
    """Try to import inkex from various sources.

    Returns the inkex module on success, or raises ImportError.
    """
    # 1. Plain import (works in venv or pip-installed)
    try:
        import inkex  # noqa: F401
        return sys.modules["inkex"]
    except ImportError:
        pass

    # 2. Inkscape bundle paths — try each root
    _inkscape_roots = set()
    _inkscape_roots.add("/Applications/Inkscape.app/Contents/Resources")
    _inkscape_roots.add(
        os.path.expanduser("~/Applications/Inkscape.app/Contents/Resources")
    )
    _inkscape_roots.add("/app")   # flatpak
    _inkscape_roots.add("/usr")   # system
    _inkscape_roots.update(
        r"C:\Program Files\Inkscape",
        r"C:\Program Files (x86)\Inkscape",
    )

    for _root in _inkscape_roots:
        _extensions = os.path.join(_root, "share", "inkscape", "extensions")
        if not os.path.isdir(_extensions):
            continue

        sys.path.insert(0, _extensions)

        # 2a. Try with just extensions path (no numpy dependency)
        try:
            import inkex  # noqa: F401
            return sys.modules["inkex"]
        except ImportError as _first_err:
            # 2b. Need numpy — add Inkscape's bundled site-packages
            _sp_count = 0
            for _lib_dir in (
                os.path.join(_root, "lib"),
                os.path.join(_root, "Resources", "lib"),
            ):
                if not os.path.isdir(_lib_dir):
                    continue
                for _py_dir in glob.glob(os.path.join(_lib_dir, "python3*")):
                    _sp = os.path.join(_py_dir, "site-packages")
                    if os.path.isdir(_sp) and _sp not in sys.path:
                        sys.path.insert(0, _sp)
                        _sp_count += 1

            if _sp_count > 0:
                try:
                    import inkex  # noqa: F401
                    return sys.modules["inkex"]
                except ImportError:
                    # Still fails — likely numpy version mismatch.
                    # Remove the site-packages we added and report clearly.
                    for _ in range(_sp_count):
                        sys.path.pop(0)

        # 2c. One last try without site-packages (in case inkex
        #     has a fallback for missing numpy)
        try:
            import inkex  # noqa: F401
            # Check if it loaded despite the earlier error
            if "inkex" in sys.modules and sys.modules["inkex"] is not None:
                return sys.modules["inkex"]
        except ImportError:
            pass

        # Clean up — remove the extensions path we added
        if sys.path and sys.path[0] == _extensions:
            sys.path.pop(0)

    raise ImportError(
        "Could not find a usable 'inkex' module. "
        "Run inside the project venv (inkmcp/venv) or "
        "install inkex with: pip install inkex"
    )


inkex = _find_and_import_inkex()

from lxml import etree

from inkmcpcli import parse_command_string, strip_python_comments
from inkmcpops.element_mapping import (
    get_element_class,
    should_place_in_defs,
    ensure_defs_section,
    get_unique_id,
)
from inkmcpops.common import get_element_info_data


# ---------------------------------------------------------------------------
#  Mock extension context
# ---------------------------------------------------------------------------

class WorkerContext:
    """Minimal mock of `inkex.EffectExtension` for headless SVG operations.

    Provides the `.svg` and `.save()` interface that the existing
    `inkmcpops/*` modules expect from an extension instance.
    """

    def __init__(self, svg_root: inkex.SvgDocumentElement,
                 document: etree.ElementTree):
        self.svg = svg_root
        self.document = document

    def save(self, f) -> None:
        """Serialize the SVG document to a file-like object."""
        self.document.write(f, pretty_print=True, xml_declaration=True)


# ---------------------------------------------------------------------------
#  Worker class
# ---------------------------------------------------------------------------

class InkscapeWorker:
    """Maintains an in-memory SVG document and processes commands."""

    def __init__(self):
        self.svg, self.document = self._create_document()
        self.ctx = WorkerContext(self.svg, self.document)

    # ── Document management ──────────────────────────────────────

    @staticmethod
    def _create_document():
        """Create a fresh SVG document with default attributes."""
        svg = inkex.SvgDocumentElement()
        svg.set("xmlns", "http://www.w3.org/2000/svg")
        svg.set("xmlns:xlink", "http://www.w3.org/1999/xlink")
        svg.set("width", "1920px")
        svg.set("height", "1080px")
        svg.set("viewBox", "0 0 1920 1080")
        svg.set("version", "1.1")

        # Add named view (for inkscape compatibility)
        namedview = inkex.NamedView()
        namedview.set("pagecolor", "#ffffff")
        namedview.set("inkscape:pageopacity", "0.0")
        namedview.set("inkscape:pagecheckerboard", "0")
        namedview.set("showgrid", "false")
        svg.append(namedview)

        # Add defs section
        defs = inkex.Defs()
        svg.append(defs)

        # Add initial layer
        layer = inkex.Layer.new("Layer 1")
        svg.append(layer)

        # Build an ElementTree for save()
        document = etree.ElementTree(svg)
        return svg, document

    def reset_document(self):
        """Replace the current document with a fresh one."""
        self.svg, self.document = self._create_document()
        self.ctx = WorkerContext(self.svg, self.document)

    def get_current_element_counts(self) -> Dict[str, int]:
        """Count elements by tag type."""
        counts: Dict[str, int] = {}
        for elem in self.svg.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            counts[tag] = counts.get(tag, 0) + 1
        return counts

    # ── Command dispatch ──────────────────────────────────────────

    def handle_command(self, command_str: str) -> Dict[str, Any]:
        """Parse and execute a single command string.

        Args:
            command_str: e.g. "rect x=100 y=100 width=200 fill=blue"

        Returns:
            A dict with "success", "data", and optionally "error".
        """
        if not command_str or not command_str.strip():
            return _error("Empty command")

        element_data = parse_command_string(command_str)
        tag = element_data.get("tag", "")

        if not tag:
            return _error("Could not parse command: no tag found")

        return self._dispatch(tag, element_data)

    def handle_command_from_tag(self, tag: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a command using pre-parsed tag and attributes.

        Args:
            tag: The command tag (e.g. "execute-code", "rect")
            attributes: Pre-parsed attributes dict

        Returns:
            A dict with "success", "data", and optionally "error".
        """
        if not tag:
            return _error("Empty tag")

        element_data = {"tag": tag, "attributes": attributes}
        return self._dispatch(tag, element_data)

    def _dispatch(self, tag: str, element_data: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch to element creation or action handler based on tag."""
        ElementClass = get_element_class(tag)

        if ElementClass is not None:
            # ── SVG element creation ──
            return self._create_element(element_data)
        else:
            # ── Action / query commands ──
            attributes = element_data.get("attributes", {})
            return self._handle_action(tag, attributes)

    # ── Element creation ──────────────────────────────────────────

    def _create_element(self, element_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create an SVG element from parsed command data."""
        try:
            id_mapping: Dict[str, str] = {}
            generated_ids: List[str] = []

            element = self._create_recursive(
                element_data, id_mapping, generated_ids
            )

            tag = element_data.get("tag", "")
            ElementClass = get_element_class(tag)

            if ElementClass and should_place_in_defs(ElementClass):
                # Place in <defs>
                defs = ensure_defs_section(self.svg)
                defs.append(element)
            else:
                # Place in current layer (or root layer)
                current_layer = self.svg.get_current_layer()
                if current_layer is not None:
                    current_layer.append(element)
                else:
                    self.svg.append(element)

            response_data: Dict[str, Any] = {
                "message": f"{tag} created successfully",
                "id": element.get("id"),
                "tag": tag,
                "attributes": dict(element.attrib),
            }

            if id_mapping:
                response_data["id_mapping"] = id_mapping
            if generated_ids:
                response_data["generated_ids"] = generated_ids

            total = len(id_mapping) + len(generated_ids)
            if total > 1:
                response_data["message"] = (
                    f"{total} elements created successfully"
                )

            return _success(**response_data)

        except Exception as e:
            return _error(f"Element creation failed: {e}",
                          traceback=traceback.format_exc())

    def _create_recursive(
        self,
        element_data: Dict[str, Any],
        id_mapping: Dict[str, str],
        generated_ids: List[str],
    ) -> inkex.BaseElement:
        """Recursively create an element and its children."""
        tag = element_data.get("tag", "")
        attributes = element_data.get("attributes", {})
        children = element_data.get("children", [])

        ElementClass = get_element_class(tag)

        if ElementClass:
            element = ElementClass()  # type: ignore
        else:
            # Fallback: raw lxml element (for filter primitives, etc.)
            element = inkex.etree.Element(tag.replace(":", "}").split("}")[-1]
                                          if ":" in tag else tag)

        # Handle ID — with collision auto-increment
        requested_id = attributes.get("id")
        if requested_id:
            actual_id = get_unique_id(self.svg, tag, requested_id)
            id_mapping[requested_id] = actual_id
        else:
            actual_id = get_unique_id(self.svg, tag, None)
            generated_ids.append(actual_id)
        element.set("id", actual_id)

        # Set all other attributes
        for attr_name, attr_value in attributes.items():
            if attr_name == "id":
                continue
            attr_set = False
            if hasattr(element, attr_name):
                try:
                    setattr(element, attr_name, attr_value)
                    attr_set = True
                except Exception:
                    pass
            if not attr_set:
                element.set(attr_name, str(attr_value))

        # Process children
        for child_data in children:
            child = self._create_recursive(
                child_data, id_mapping, generated_ids
            )
            element.append(child)

        return element

    # ── Action dispatch ──────────────────────────────────────────

    def _handle_action(self, tag: str,
                       attributes: Dict[str, Any]) -> Dict[str, Any]:
        """Handle non-element commands (get-info, execute-code, etc.)."""
        try:
            if tag == "execute-code":
                return self._execute_code(attributes)
            elif tag == "get-info":
                return self._get_document_info()
            elif tag == "get-selection":
                return self._get_selection_info()
            elif tag == "get-info-by-id":
                return self._get_element_info(
                    attributes.get("id", "")
                )
            elif tag == "export-document-image":
                return self._export_document_image(attributes)
            else:
                return _error(f"Unknown command: {tag}")
        except Exception as e:
            return _error(f"Action '{tag}' failed: {e}",
                          traceback=traceback.format_exc())

    def _execute_code(self, attributes: Dict[str, Any]) -> Dict[str, Any]:
        """Execute arbitrary Python/inkex code in the SVG context."""
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from inkmcpops.execute_operations import execute_code

        result = execute_code(self.ctx, self.svg, attributes)

        # Normalize: convert old inkmcpops format (status/data) to new format
        if "status" in result and "success" not in result:
            if result.get("status") == "success":
                # Mark as top-level success but keep the inner data intact
                data = result.get("data", {})
                return _success(data.get("message", "Code executed"), **{
                    k: v for k, v in data.items() if k != "message"
                })
            else:
                data = result.get("data", {})
                return _error(data.get("error", "Code execution failed"))

        return result

    def _get_document_info(self) -> Dict[str, Any]:
        """Return document dimensions and element counts."""
        viewbox = self.svg.get("viewBox", "0 0 100 100").split()
        width = self.svg.get("width", "unknown")
        height = self.svg.get("height", "unknown")

        return _success(
            message="Document information",
            dimensions={"width": width, "height": height},
            viewBox=viewbox,
            elementCounts=self.get_current_element_counts(),
        )

    def _get_selection_info(self) -> Dict[str, Any]:
        """In headless mode selection is empty by default."""
        # Elements can be "selected" by ID via query params
        return _success(
            message="No selection (headless mode)",
            count=0,
            elements=[],
        )

    def _get_element_info(self, element_id: str) -> Dict[str, Any]:
        """Get information about a specific element by ID."""
        element = self.svg.getElementById(element_id)
        if element is None:
            return _error(f"Element not found: {element_id}")

        element_info = get_element_info_data(element)
        return _success(
            message=f"Element information for {element_id}",
            **element_info,
        )

    def _export_document_image(self, attributes: Dict[str, Any]) -> Dict[str, Any]:
        """Export the document as a PNG image.

        Uses Inkscape CLI for rendering. Falls back if Inkscape is not installed.
        """
        fmt = attributes.get("format", "png")
        max_size = int(attributes.get("max_size", 0))
        return_base64 = str(attributes.get("return_base64", "true")).lower() == "true"
        area = attributes.get("area", "page")

        if fmt != "png":
            return _error(f"Unsupported format: {fmt}")

        # Find Inkscape binary
        inkscape_bin = self._find_inkscape()

        if not inkscape_bin:
            return _error(
                "Inkscape CLI not found. Install Inkscape or use "
                "execute-code to export as SVG."
            )

        try:
            # Save current document to temp SVG file
            svg_fd, svg_path = tempfile.mkstemp(suffix=".svg")
            os.close(svg_fd)
            with open(svg_path, "wb") as f:
                self.document.write(f, pretty_print=True, xml_declaration=True)

            # Build export path
            out_fd, out_path = tempfile.mkstemp(suffix=f".{fmt}")
            os.close(out_fd)

            # Build Inkscape CLI args
            cmd = [inkscape_bin, f"--export-filename={out_path}"]

            if area == "drawing":
                cmd.append("--export-area-drawing")
            elif area == "page":
                cmd.append("--export-area-page")

            if max_size > 0:
                width = float(
                    self.svg.get("width", "800").replace("px", "").replace("mm", "")
                )
                if width > 0:
                    dpi = int((max_size / width) * 96)
                    cmd.append(f"--export-dpi={dpi}")

            cmd.append(svg_path)

            subprocess.run(cmd, check=True, capture_output=True, text=True,
                           timeout=60)

            # Clean up temp SVG
            os.unlink(svg_path)

            file_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0

            response_data: Dict[str, Any] = {
                "export_path": out_path,
                "format": fmt,
                "file_size": file_size,
                "area": area,
            }

            if return_base64 and os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    import base64
                    response_data["base64_data"] = base64.b64encode(
                        f.read()
                    ).decode("utf-8")

            return _success(
                f"Document exported as {fmt.upper()}",
                **response_data,
            )

        except subprocess.TimeoutExpired:
            return _error("Inkscape export timed out")
        except subprocess.CalledProcessError as e:
            return _error(f"Inkscape export failed: {e.stderr or e.stdout}")
        except Exception as e:
            return _error(f"Export failed: {e}")

    @staticmethod
    def _find_inkscape() -> Optional[str]:
        """Locate the Inkscape CLI binary on any platform."""
        # 1. Try PATH
        inkscape = shutil.which("inkscape")
        if inkscape:
            return inkscape

        system = platform.system()

        # 2. macOS
        if system == "Darwin":
            candidates = [
                "/Applications/Inkscape.app/Contents/MacOS/inkscape",
                os.path.expanduser(
                    "~/Applications/Inkscape.app/Contents/MacOS/inkscape"
                ),
            ]
            for c in candidates:
                if os.path.exists(c):
                    return c

        # 3. Windows
        if system == "Windows":
            program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            program_files_x86 = os.environ.get(
                "ProgramFiles(x86)", r"C:\Program Files (x86)"
            )
            candidates = [
                os.path.join(program_files, "Inkscape", "bin", "inkscape.exe"),
                os.path.join(program_files_x86, "Inkscape", "bin", "inkscape.exe"),
            ]
            for c in candidates:
                if os.path.exists(c):
                    return c

        return None


# ---------------------------------------------------------------------------
#  Response helpers
# ---------------------------------------------------------------------------

def _success(message: str, **data) -> Dict[str, Any]:
    response_data: Dict[str, Any] = {"message": message}
    response_data.update(data)
    return {"success": True, "status": "success", "data": response_data}


def _error(message: str, **data) -> Dict[str, Any]:
    response_data: Dict[str, Any] = {"error": message}
    response_data.update(data)
    return {"success": False, "status": "error", "data": response_data}


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------

def main():
    """Read JSON-line commands from stdin, write results to stdout."""
    worker = InkscapeWorker()

    # Signal readiness (Rust server waits for this)
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            response = {"id": None, "error": f"Invalid JSON: {e}"}
            print(json.dumps(response), flush=True)
            continue

        req_id = request.get("id")
        command = request.get("command", "")
        tag = request.get("tag")

        # Handle structured request (tag + attributes directly, no command string parsing)
        if tag:
            attributes = request.get("attributes", {})
            result = worker.handle_command_from_tag(tag, attributes)
            response = {"id": req_id, **result}
            print(json.dumps(response), flush=True)
            continue

        # Handle shutdown command
        if command == "__shutdown__":
            response = {"id": req_id, "success": True,
                        "data": {"message": "Shutting down"}}
            print(json.dumps(response), flush=True)
            break

        # Handle reset command
        if command == "__reset__":
            worker.reset_document()
            response = {"id": req_id, "success": True,
                        "data": {"message": "Document reset"}}
            print(json.dumps(response), flush=True)
            continue

        # Execute the command
        result = worker.handle_command(command)
        response = {"id": req_id, **result}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
