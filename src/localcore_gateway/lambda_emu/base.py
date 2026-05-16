"""``LambdaInvoker`` interface shared by the native and SAM backends."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InvokeResult:
    """Outcome of one Lambda invocation, mirroring AWS invoke semantics."""

    payload: Any
    """The handler's return value (parsed JSON), or the Lambda error envelope."""

    function_error: str | None = None
    """``"Unhandled"`` / ``"Handled"`` when the function errored, else ``None``
    (the same signal AWS returns via the ``X-Amz-Function-Error`` header)."""

    logs: list[str] = field(default_factory=list)
    """CloudWatch-style log lines (START / handler output / END / REPORT)."""

    @property
    def errored(self) -> bool:
        return self.function_error is not None


class LambdaInvoker(abc.ABC):
    """A local stand-in for the AWS Lambda invoke API."""

    @abc.abstractmethod
    async def invoke(
        self,
        event: Any,
        *,
        client_context: dict[str, Any] | None = None,
    ) -> InvokeResult:
        """Invoke with ``event`` as the payload.

        ``client_context`` becomes ``context.client_context.custom`` -- this is
        how AgentCore Gateway delivers ``bedrockAgentCoreToolName``.
        """

    async def aclose(self) -> None:  # noqa: B027  # optional no-op hook
        """Release backend resources (override if needed)."""


def make_invoker(spec: Any, *, code_root: str | None = None) -> LambdaInvoker:
    """Construct the configured backend.

    ``spec`` is a :class:`localcore_gateway.config.LambdaFunctionConfig`.
    ``code_root`` is the resolved import root for the native backend.
    """
    from localcore_gateway.lambda_emu.native import NativeLambdaInvoker
    from localcore_gateway.lambda_emu.sam import SamLambdaInvoker

    if spec.backend == "native":
        return NativeLambdaInvoker(spec, code_root=code_root)
    if spec.backend == "sam":
        return SamLambdaInvoker(spec)
    raise ValueError(f"unknown lambda backend: {spec.backend!r}")
