"""Shared pytest fixtures."""

from __future__ import annotations

import contextlib
import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SITE_DIR = FIXTURES_DIR / "site"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # keep pytest output clean
        pass


class _RecordingHandler(_QuietHandler):
    """Records the path of every GET request on `server.requests`."""

    def do_GET(self):
        self.server.requests.append(self.path)
        super().do_GET()


@contextlib.contextmanager
def _serve(directory: Path, handler_cls):
    handler = functools.partial(handler_cls, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(scope="session")
def fixture_server():
    """Serve tests/fixtures/ on a random localhost port; yields the base URL."""
    with _serve(FIXTURES_DIR, _QuietHandler) as server:
        host, port = server.server_address
        yield f"http://{host}:{port}"


@pytest.fixture(scope="session")
def _site_server_session():
    with _serve(SITE_DIR, _RecordingHandler) as server:
        yield server


@pytest.fixture
def site_server(_site_server_session):
    """Serve tests/fixtures/site/ (a small linked site with its own robots.txt).

    Yields an object with `.url` (base URL) and `.requests` (paths requested
    during this test, in order).
    """
    server = _site_server_session
    server.requests.clear()
    host, port = server.server_address
    return SimpleNamespace(url=f"http://{host}:{port}", requests=server.requests)
