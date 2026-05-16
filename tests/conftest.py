from __future__ import annotations

import sys

import pytest


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
