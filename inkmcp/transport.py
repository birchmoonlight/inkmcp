"""
Transport abstraction for communicating with the Rust MCP Server (inkmcpd).

Provides a simple JSON-line protocol over TCP (127.0.0.1:9999) for
CLI tools, Blender addon, and other Python code that needs to send
commands to the SVG worker without going through the MCP protocol.
"""

import json
import logging
import socket
from typing import Any, Dict, Optional

logger = logging.getLogger("InkmcpTransport")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9999


class TransportError(Exception):
    """Raised on communication failures with the Rust server."""


class TcpTransport:
    """TCP client that talks to inkmcpd's JSON-line socket."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 connect: bool = True):
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        if connect:
            self.connect()

    def connect(self) -> None:
        """Open a TCP connection to the Rust server."""
        if self._sock is not None:
            return
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(10.0)
            self._sock.connect((self.host, self.port))
            logger.debug("Connected to inkmcpd at %s:%s", self.host, self.port)
        except (socket.timeout, ConnectionRefusedError) as e:
            self._sock = None
            raise TransportError(
                f"Cannot connect to inkmcpd at {self.host}:{self.port}. "
                f"Is the Rust MCP Server running?\n  {e}"
            ) from e

    def disconnect(self) -> None:
        """Close the connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            logger.debug("Disconnected from inkmcpd")

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def send_command(self, command: str) -> Dict[str, Any]:
        """Send a command and return the parsed JSON response.

        Args:
            command: The command string, e.g. "rect x=100 y=100 ..."

        Returns:
            Response dict with "success" and "data" keys.
        """
        if self._sock is None:
            raise TransportError("Not connected. Call connect() first.")

        # Send the command as a JSON line
        request = json.dumps({"command": command}) + "\n"
        try:
            self._sock.sendall(request.encode("utf-8"))
        except (OSError, BrokenPipeError) as e:
            self._sock = None
            raise TransportError(f"Write failed (connection lost): {e}") from e

        # Read the response (one JSON line)
        try:
            buf = []
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    self._sock = None
                    raise TransportError(
                        "Connection closed by server while reading response"
                    )
                buf.append(chunk)
                # Check if we have a complete line
                data = b"".join(buf)
                if b"\n" in data:
                    line, _ = data.split(b"\n", 1)
                    return json.loads(line.decode("utf-8"))
        except socket.timeout as e:
            raise TransportError(f"Read timeout: {e}") from e
        except (OSError, ConnectionResetError) as e:
            self._sock = None
            raise TransportError(f"Read failed: {e}") from e

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()


def send_command(command: str, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT) -> Dict[str, Any]:
    """One-shot helper: connect, send, disconnect.

    Args:
        command: Command string to execute.
        host: TCP host (default 127.0.0.1).
        port: TCP port (default 9999).

    Returns:
        Parsed response dict.
    """
    with TcpTransport(host, port, connect=True) as t:
        return t.send_command(command)
