from __future__ import annotations

import contextlib
import socket
import sys
import threading
import time
from collections.abc import Iterator

# Pre-import so it lands in _isolate_imports' baseline snapshot: httpx lazily
# imports httpcore on the FIRST real request and maps its exceptions by class
# identity. If that first import happened inside a test, isolation would evict
# httpcore and later tests would see unmapped (re-imported) httpcore errors.
import httpcore  # noqa: F401
import pytest
import uvicorn


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def serve_asgi(app) -> Iterator[str]:
    """Serve an ASGI app on an ephemeral port (uvicorn thread); yields the base URL."""
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _isolate_imports():
    """Isolate sys.modules / sys.path per test.

    The native backend loads handler code into the running process. Tests
    reuse generic module names (``h``, ``helper``) across files; without
    isolation a cached module from one test would leak into the next.
    """
    mods = set(sys.modules)
    path = list(sys.path)
    yield
    for name in set(sys.modules) - mods:
        del sys.modules[name]
    sys.path[:] = path
