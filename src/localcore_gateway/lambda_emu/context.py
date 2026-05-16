"""Faithful AWS Lambda ``context`` object.

Mirrors the shape AWS injects so unmodified handler code works, including the
``client_context.custom`` dict where AgentCore Gateway passes
``bedrockAgentCoreToolName``.
"""

from __future__ import annotations

import time
from typing import Any


class CognitoIdentity:
    __slots__ = ("cognito_identity_id", "cognito_identity_pool_id")

    def __init__(self) -> None:
        self.cognito_identity_id: str | None = None
        self.cognito_identity_pool_id: str | None = None


class ClientContext:
    """AWS ``context.client_context`` (populated from the invoke ClientContext)."""

    __slots__ = ("client", "custom", "env")

    def __init__(
        self,
        custom: dict[str, Any] | None = None,
        env: dict[str, Any] | None = None,
        client: dict[str, Any] | None = None,
    ) -> None:
        self.custom: dict[str, Any] = custom or {}
        self.env: dict[str, Any] = env or {}
        self.client: dict[str, Any] = client or {}


class LambdaContext:
    """The object Lambda passes as the handler's second argument."""

    def __init__(
        self,
        *,
        function_name: str,
        function_version: str = "$LATEST",
        invoked_function_arn: str,
        memory_limit_in_mb: int = 128,
        aws_request_id: str,
        log_group_name: str,
        log_stream_name: str,
        deadline_ms: float,
        client_context: ClientContext | None = None,
    ) -> None:
        self.function_name = function_name
        self.function_version = function_version
        self.invoked_function_arn = invoked_function_arn
        # AWS exposes this as a string.
        self.memory_limit_in_mb = str(memory_limit_in_mb)
        self.aws_request_id = aws_request_id
        self.log_group_name = log_group_name
        self.log_stream_name = log_stream_name
        self.identity = CognitoIdentity()
        self.client_context = client_context
        self._deadline_ms = deadline_ms

    def get_remaining_time_in_millis(self) -> int:
        return max(0, int(self._deadline_ms - time.time() * 1000.0))
