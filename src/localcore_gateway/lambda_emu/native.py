"""In-process local Lambda backend (default; no Docker).

Faithful enough that unmodified handler code runs unchanged:

* AWS-shaped ``event`` / ``context`` (incl. ``client_context.custom``)
* CloudWatch-style ``START`` / ``END`` / ``REPORT`` log framing
* Lambda error envelope ``{errorMessage, errorType, stackTrace}``
* cold start on handler-file change (hot reload)

Documented caveat: a sync handler thread cannot be hard-killed in-process, so
``timeout_sec`` is a *soft* timeout here -- the invoke returns the Lambda
timeout envelope but a stuck thread keeps running in the background. Use the
``sam`` backend when you need true isolation / hard kill.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import inspect
import io
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

from localcore_gateway.config import LambdaFunctionConfig
from localcore_gateway.lambda_emu.base import InvokeResult, LambdaInvoker
from localcore_gateway.lambda_emu.context import ClientContext, LambdaContext


class _HandlerRef:
    """Resolves + hot-reloads a Lambda handler callable."""

    def __init__(self, handler: str, code_root: str) -> None:
        self._spec = handler
        self._code_root = code_root
        self._mod = None
        self._file: Path | None = None
        self._attr: str = ""
        self._load()

    def _ensure_path(self) -> None:
        if self._code_root not in sys.path:
            sys.path.insert(0, self._code_root)

    def _load(self) -> None:
        self._ensure_path()
        if ":" in self._spec:
            file_part, attr = self._spec.split(":", 1)
            file_path = (Path(self._code_root) / file_part).resolve()
            modname = f"_lcgw_handler_{file_path.stem}"
            spec = importlib.util.spec_from_file_location(modname, file_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load handler file: {file_path}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            self._mod, self._attr, self._file = mod, attr, file_path
        else:
            modpath, attr = self._spec.rsplit(".", 1)
            mod = importlib.import_module(modpath)
            self._mod, self._attr = mod, attr
            self._file = Path(mod.__file__) if getattr(mod, "__file__", None) else None
        self._mtime = self._file.stat().st_mtime if self._file else 0.0

    def maybe_reload(self) -> bool:
        """Cold-start the handler if its source changed. Returns True on reload."""
        if not self._file or not self._file.exists():
            return False
        mtime = self._file.stat().st_mtime
        if mtime <= self._mtime:
            return False
        if ":" in self._spec:
            self._load()
        else:
            self._mod = importlib.reload(self._mod)
            self._mtime = mtime
        return True

    def resolve(self) -> Callable[..., Any]:
        fn = getattr(self._mod, self._attr, None)
        if not callable(fn):
            raise AttributeError(
                f"handler {self._attr!r} not found / not callable in {self._spec}"
            )
        return fn


class NativeLambdaInvoker(LambdaInvoker):
    def __init__(self, cfg: LambdaFunctionConfig, *, code_root: str | None = None) -> None:
        self._cfg = cfg
        self._handler = _HandlerRef(cfg.handler, code_root or cfg.code_root or ".")
        self._lock = asyncio.Lock()  # one warm execution environment

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
            cold = self._handler.maybe_reload()
            if cold:
                logs.append(f"INIT_START Runtime Version: python (cold start)")
            fn = self._handler.resolve()

            arn = (
                f"arn:aws:lambda:{cfg.region}:000000000000:"
                f"function:{cfg.function_name}"
            )
            ctx = LambdaContext(
                function_name=cfg.function_name,
                invoked_function_arn=arn,
                memory_limit_in_mb=cfg.memory_mb,
                aws_request_id=req_id,
                log_group_name=f"/aws/lambda/{cfg.function_name}",
                log_stream_name=time.strftime("%Y/%m/%d/[$LATEST]") + req_id.replace("-", ""),
                deadline_ms=time.time() * 1000.0 + cfg.timeout_sec * 1000.0,
                client_context=ClientContext(custom=dict(client_context or {})),
            )

            logs.append(f"START RequestId: {req_id} Version: $LATEST")
            buf = io.StringIO()
            start = time.perf_counter()
            payload: Any
            function_error: str | None = None

            prev_env = _apply_env(cfg)
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    payload = await self._call(fn, event, ctx, cfg.timeout_sec)
            except _Timeout:
                function_error = "Unhandled"
                payload = {
                    "errorMessage": f"{req_id} Task timed out after "
                    f"{cfg.timeout_sec:.2f} seconds",
                    "errorType": "TimeoutError",
                }
                buf.write(
                    f"\n{time.strftime('%Y-%m-%dT%H:%M:%SZ')} {req_id} Task "
                    f"timed out after {cfg.timeout_sec:.2f} seconds\n"
                )
            except BaseException as exc:  # handler raised -> Lambda error envelope
                function_error = "Unhandled"
                payload = {
                    "errorMessage": str(exc),
                    "errorType": type(exc).__name__,
                    "stackTrace": traceback.format_tb(exc.__traceback__),
                }
                buf.write(
                    "".join(traceback.format_exception(exc)) + "\n"
                )
            finally:
                _restore_env(prev_env)

            dur_ms = (time.perf_counter() - start) * 1000.0
            for line in buf.getvalue().splitlines():
                logs.append(line)
            logs.append(f"END RequestId: {req_id}")
            logs.append(
                f"REPORT RequestId: {req_id} "
                f"Duration: {dur_ms:.2f} ms "
                f"Billed Duration: {max(1, round(dur_ms))} ms "
                f"Memory Size: {cfg.memory_mb} MB "
                f"Max Memory Used: {_max_rss_mb()} MB"
            )
            return InvokeResult(
                payload=payload, function_error=function_error, logs=logs
            )

    async def _call(
        self, fn: Callable[..., Any], event: Any, ctx: LambdaContext, timeout: float
    ) -> Any:
        if inspect.iscoroutinefunction(fn):
            try:
                return await asyncio.wait_for(fn(event, ctx), timeout)
            except asyncio.TimeoutError:
                raise _Timeout from None
        # sync handler: offload; cannot hard-cancel the thread (soft timeout).
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, event, ctx), timeout
            )
        except asyncio.TimeoutError:
            raise _Timeout from None


class _Timeout(Exception):
    pass


def _apply_env(cfg: LambdaFunctionConfig) -> dict[str, str | None]:
    overrides = {
        "AWS_LAMBDA_FUNCTION_NAME": cfg.function_name,
        "AWS_LAMBDA_FUNCTION_VERSION": "$LATEST",
        "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": str(cfg.memory_mb),
        "AWS_LAMBDA_FUNCTION_TIMEOUT": str(int(cfg.timeout_sec)),
        "AWS_REGION": cfg.region,
        "AWS_DEFAULT_REGION": cfg.region,
        **cfg.env,
    }
    prev: dict[str, str | None] = {}
    for k, v in overrides.items():
        prev[k] = os.environ.get(k)
        os.environ[k] = v
    return prev


def _restore_env(prev: dict[str, str | None]) -> None:
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _max_rss_mb() -> int:
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS reports bytes.
        return int(rss / (1024 if sys.platform != "darwin" else 1024 * 1024))
    except Exception:
        return 0
