"""Subprocess Lambda runtime (one per target = one warm execution env).

Launched by the native backend by absolute file path
(``<python> <this_file> --fd N``) -- NOT ``-m`` -- so the target's chosen
interpreter need not have localcore-gateway installed. This module is
stdlib-only and self-contained. The parent and this process talk
length-prefixed JSON over the inherited socket fd ``N``.

Isolation is the whole point: this process's ``sys.path`` is ONLY the target's
code roots (plus the chosen interpreter's stdlib/site), so two targets whose
handlers share a top-level module name (the monorepo case: every service has
``handler.py``) never collide -- they live in different interpreters, and each
target can use its own venv (its own deps + Python version).

Protocol
--------
parent -> worker  ``{"op":"init","handler":..,"code_roots":[..]}``
worker -> parent  ``{"ok":true}`` | ``{"ok":false,"error":{..}}``
parent -> worker  ``{"op":"invoke","event":..,"client_context":{..},"ctx":{..}}``
worker -> parent  ``{"ok":bool,"payload":..,"function_error":str|None,
                     "output":str,"max_rss_mb":int}``
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import inspect
import io
import json
import socket
import struct
import sys
import traceback
from pathlib import Path
from typing import Any


def _recv(sock: socket.socket) -> dict[str, Any] | None:
    hdr = _recvn(sock, 4)
    if hdr is None:
        return None
    (length,) = struct.unpack(">I", hdr)
    body = _recvn(sock, length)
    if body is None:
        return None
    return json.loads(body)


def _recvn(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _send(sock: socket.socket, obj: dict[str, Any]) -> None:
    body = json.dumps(obj, default=str).encode()
    sock.sendall(struct.pack(">I", len(body)) + body)


def _safe_resolve(p: str) -> str:
    try:
        return str(Path(p).resolve())
    except OSError:
        return p


def _resolve_handler(spec: str) -> Any:
    if ":" in spec:
        file_part, attr = spec.split(":", 1)
        path = next(
            (p for p in (Path(r) / file_part for r in sys.path) if p.exists()),
            Path(file_part),
        ).resolve()
        modname = f"_lcgw_handler_{path.stem}"
        ms = importlib.util.spec_from_file_location(modname, path)
        if ms is None or ms.loader is None:
            raise ImportError(f"cannot load handler file: {path}")
        mod = importlib.util.module_from_spec(ms)
        sys.modules[modname] = mod
        ms.loader.exec_module(mod)
    else:
        modpath, attr = spec.rsplit(".", 1)
        mod = importlib.import_module(modpath)
    fn = getattr(mod, attr, None)
    if not callable(fn):
        raise AttributeError(f"handler {attr!r} not found / not callable in {spec}")  # noqa: TRY004
    return fn


def _max_rss_mb() -> int:
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(rss / (1024 if sys.platform != "darwin" else 1024 * 1024))
    except Exception:  # noqa: BLE001
        return 0


class _Context:
    """Minimal AWS Lambda context (the subprocess builds it from ctx fields)."""

    def __init__(self, c: dict[str, Any]) -> None:
        self.function_name = c["function_name"]
        self.function_version = "$LATEST"
        self.invoked_function_arn = c["arn"]
        self.memory_limit_in_mb = str(c["memory_mb"])
        self.aws_request_id = c["aws_request_id"]
        self.log_group_name = f"/aws/lambda/{c['function_name']}"
        self.log_stream_name = c["log_stream"]
        self._deadline_ms = c["deadline_ms"]
        cc = c.get("client_context") or {}
        self.client_context = type("ClientContext", (), {"custom": cc, "env": {}, "client": {}})()
        self.identity = type(
            "CognitoIdentity",
            (),
            {"cognito_identity_id": None, "cognito_identity_pool_id": None},
        )()

    def get_remaining_time_in_millis(self) -> int:
        import time

        return max(0, int(self._deadline_ms - time.time() * 1000.0))


def _invoke(fn: Any, event: Any, ctx: _Context) -> dict[str, Any]:
    buf = io.StringIO()
    payload: Any
    function_error: str | None = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if inspect.iscoroutinefunction(fn):
                import asyncio

                payload = asyncio.run(fn(event, ctx))
            else:
                payload = fn(event, ctx)
    except BaseException as exc:  # noqa: BLE001  # Lambda: any failure -> envelope
        function_error = "Unhandled"
        payload = {
            "errorMessage": str(exc),
            "errorType": type(exc).__name__,
            "stackTrace": traceback.format_tb(exc.__traceback__),
        }
        buf.write("".join(traceback.format_exception(exc)))
    return {
        "ok": function_error is None,
        "payload": payload,
        "function_error": function_error,
        "output": buf.getvalue(),
        "max_rss_mb": _max_rss_mb(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fd", type=int, required=True)
    fd = ap.parse_args().fd
    sock = socket.socket(fileno=fd)

    init = _recv(sock)
    if init is None or init.get("op") != "init":
        return
    try:
        # Drop our own dir (added by launching this file directly) so the
        # handler's namespace is only stdlib + interpreter site + code_roots.
        here = str(Path(__file__).resolve().parent)
        sys.path[:] = [p for p in sys.path if p and _safe_resolve(p) != here]
        for root in reversed(init["code_roots"]):
            if root not in sys.path:
                sys.path.insert(0, root)
        fn = _resolve_handler(init["handler"])
        _send(sock, {"ok": True})
    except BaseException as exc:  # noqa: BLE001  # report init failure, then exit
        _send(
            sock,
            {
                "ok": False,
                "error": {
                    "errorMessage": str(exc),
                    "errorType": type(exc).__name__,
                    "stackTrace": traceback.format_tb(exc.__traceback__),
                },
            },
        )
        return

    while True:
        req = _recv(sock)
        if req is None or req.get("op") != "invoke":
            return
        ctx = _Context(req["ctx"])
        with contextlib.suppress(BrokenPipeError, ConnectionError):
            _send(sock, _invoke(fn, req["event"], ctx))


if __name__ == "__main__":
    main()
