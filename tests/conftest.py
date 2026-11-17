"""Shared pytest fixtures."""

from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="session")
def fixture_server():
    """Serve tests/fixtures/ on a random localhost port; yields the base URL."""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
