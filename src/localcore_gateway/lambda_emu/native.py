"""Native local Lambda backend: one subprocess worker per target.

Each target's Lambda runs in its **own Python subprocess** (= one warm Lambda
execution environment). Consequences:

* True isolation -- the worker's ``sys.path`` / ``sys.modules`` contain only
  that target's code roots, so two targets whose handlers share a top-level
  module name (the monorepo case: every service has ``handler.py``) never
  collide. The gateway process's ``sys.path`` is never mutated.
* Faithful ``event`` / ``context`` (incl. ``client_context.custom``),
  CloudWatch-style ``START`` / ``END`` / ``REPORT``, Lambda error envelope.
* **Hard** timeout: a stuck handler's process is killed and respawned.
* Hot reload: any ``*.py`` change under the code roots respawns the worker
  (a real cold start -- fresh interpreter).

No Docker. The ``sam`` backend remains for full Linux-runtime fidelity.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from localcore_gateway.config import LambdaFunctionConfig
from localcore_gateway.lambda_emu._worker import _recv, _send
from localcore_gateway.lambda_emu.base import InvokeResult, LambdaInvoker

_IGNORE_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
_SPAWN_TIMEOUT_S = 30.0
# Launched by absolute file path (NOT `-m`): the target's interpreter need not
# have localcore-gateway installed. _worker.py is stdlib-only and standalone.
_WORKER_PY = str(Path(__file__).with_name("_worker.py"))


def _scan_mtime(roots: list[str]) -> float:
    """Newest *.py mtime under the code roots (drives cold-start detection)."""
    latest = 0.0
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            parts = py.relative_to(base).parts[:-1]
            if any(p in _IGNORE_DIRS or p.startswith(".") for p in parts):
                continue
            with suppress(OSError):
                latest = max(latest, py.stat().st_mtime)
    return latest


def _parse_env_file(path: str) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE per line; #-comments; optional quotes."""
    out: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def _child_env(cfg: LambdaFunctionConfig, env_file: str | None) -> dict[str, str]:
    # Built once; passed to the subprocess. Precedence: process env <
    # AWS_* faithful defaults < env_file < inline `env`.
    env = dict(os.environ)
    env.update(
        {
            "AWS_LAMBDA_FUNCTION_NAME": cfg.function_name,
            "AWS_LAMBDA_FUNCTION_VERSION": "$LATEST",
            "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": str(cfg.memory_mb),
            "AWS_LAMBDA_FUNCTION_TIMEOUT": str(int(cfg.timeout_sec)),
            "AWS_REGION": cfg.region,
            "AWS_DEFAULT_REGION": cfg.region,
        }
    )
    if env_file:
        env.update(_parse_env_file(env_file))
    env.update(cfg.env)
    return env


class NativeLambdaInvoker(LambdaInvoker):
    def __init__(
        self,
        cfg: LambdaFunctionConfig,
        *,
        code_roots: list[str] | None = None,
        env_file: str | None = None,
        python: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._roots = [str(Path(r).resolve()) for r in (code_roots or ["."])]
        self._python = python or sys.executable
        self._env = _child_env(cfg, env_file)
        self._lock = asyncio.Lock()  # one warm execution environment
        self._proc: asyncio.subprocess.Process | None = None
        self._sock: socket.socket | None = None
        self._mtime = 0.0

    async def _spawn(self) -> None:
        parent, child = socket.socketpair()
        child.set_inheritable(True)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._python,
                _WORKER_PY,
                "--fd",
                str(child.fileno()),
                pass_fds=(child.fileno(),),
                env=self._env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as err:
            parent.close()
            raise RuntimeError(f"cannot launch worker with python {self._python!r}: {err}") from err
        finally:
            child.close()
        parent.setblocking(True)  # used via blocking to_thread txns
        init = {"op": "init", "handler": self._cfg.handler, "code_roots": self._roots}
        try:
            resp = await asyncio.wait_for(asyncio.to_thread(self._txn, parent, init), _SPAWN_TIMEOUT_S)
        except TimeoutError:
            resp = None
        if not resp or not resp.get("ok"):
            parent.close()
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            err = (resp or {}).get("error", {}) if resp else {}
            raise RuntimeError(
                f"handler init failed: {err.get('errorType', 'WorkerStartError')}: "
                f"{err.get('errorMessage', 'worker did not start')}"
            )
        self._proc, self._sock = proc, parent
        self._mtime = _scan_mtime(self._roots)

    @staticmethod
    def _txn(sock: socket.socket, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Blocking send+recv (runs in a thread). None = worker gone."""
        try:
            _send(sock, frame)
            return _recv(sock)
        except (OSError, ConnectionError):
            return None

    async def _kill(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._proc is not None:
            with suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 2.0)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self._proc.kill()
                await self._proc.wait()
            self._proc = None

    async def _stderr_tail(self) -> str:
        if self._proc is None or self._proc.stderr is None:
            return ""
        with suppress(Exception):
            data = await asyncio.wait_for(self._proc.stderr.read(), 1.0)
            return data.decode(errors="replace")[-2000:]
        return ""

    async def invoke(
        self,
        event: Any,
        *,
        client_context: dict[str, Any] | None = None,
    ) -> InvokeResult:
        cfg = self._cfg
        req_id = str(uuid.uuid4())
        logs: list[str] = []
        async with self._lock:
            alive = self._proc is not None and self._proc.returncode is None
            changed = alive and _scan_mtime(self._roots) > self._mtime
            if not alive or changed:
                await self._kill()
                await self._spawn()
                logs.append("INIT_START Runtime Version: python (cold start)")

            arn = f"arn:aws:lambda:{cfg.region}:000000000000:function:{cfg.function_name}"
            log_stream = time.strftime("%Y/%m/%d/[$LATEST]") + req_id.replace("-", "")
            frame = {
                "op": "invoke",
                "event": event,
                "client_context": dict(client_context or {}),
                "ctx": {
                    "function_name": cfg.function_name,
                    "arn": arn,
                    "memory_mb": cfg.memory_mb,
                    "aws_request_id": req_id,
                    "log_stream": log_stream,
                    "deadline_ms": time.time() * 1000.0 + cfg.timeout_sec * 1000.0,
                    "client_context": dict(client_context or {}),
                },
            }

            logs.append(f"START RequestId: {req_id} Version: $LATEST")
            start = time.perf_counter()
            function_error: str | None = None
            output = ""
            max_rss = 0
            try:
                resp = await asyncio.wait_for(
                    asyncio.to_thread(self._txn, self._sock, frame),
                    cfg.timeout_sec,
                )
            except TimeoutError:
                resp = None
                function_error = "Unhandled"
                payload: Any = {
                    "errorMessage": f"{req_id} Task timed out after {cfg.timeout_sec:.2f} seconds",
                    "errorType": "TimeoutError",
                }
                output = f"{req_id} Task timed out after {cfg.timeout_sec:.2f} seconds\n"
                await self._kill()  # hard kill (true Lambda termination)

            if function_error is None:
                if resp is None:  # worker crashed
                    tail = await self._stderr_tail()
                    await self._kill()
                    function_error = "Unhandled"
                    payload = {
                        "errorMessage": "worker exited before responding",
                        "errorType": "WorkerCrash",
                    }
                    output = tail
                else:
                    payload = resp["payload"]
                    function_error = resp["function_error"]
                    output = resp.get("output", "")
                    max_rss = resp.get("max_rss_mb", 0)

            dur_ms = (time.perf_counter() - start) * 1000.0
            logs.extend(output.splitlines())
            logs.append(f"END RequestId: {req_id}")
            logs.append(
                f"REPORT RequestId: {req_id} "
                f"Duration: {dur_ms:.2f} ms "
                f"Billed Duration: {max(1, round(dur_ms))} ms "
                f"Memory Size: {cfg.memory_mb} MB "
                f"Max Memory Used: {max_rss} MB"
            )
            return InvokeResult(payload=payload, function_error=function_error, logs=logs)

    async def aclose(self) -> None:
        await self._kill()
