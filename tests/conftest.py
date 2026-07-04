from __future__ import annotations

import sys

# Pre-import so it lands in _isolate_imports' baseline snapshot: httpx lazily
# imports httpcore on the FIRST real request and maps its exceptions by class
# identity. If that first import happened inside a test, isolation would evict
# httpcore and later tests would see unmapped (re-imported) httpcore errors.
import httpcore  # noqa: F401
import pytest

# The uvicorn-on-a-thread plumbing is the PUBLIC testing module now
# (localcore_gateway.testing); re-exported so existing test modules keep
# their `from conftest import ...` and the suite exercises the public code.
from localcore_gateway.testing import free_port, serve_asgi  # noqa: F401


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
