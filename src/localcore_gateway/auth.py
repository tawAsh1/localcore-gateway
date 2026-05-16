"""Inbound authorizer, mirroring AgentCore Gateway authorizer modes.

AgentCore supports OAuth(JWT) / IAM / authenticate-only / none. We implement
the two that matter locally: ``none`` (dev default) and ``jwt`` (OAuth JWT),
reusing FastMCP's ``JWTVerifier`` rather than hand-verifying tokens.
"""

from __future__ import annotations

from typing import Any

from localcore_gateway.config import AuthConfig


def build_auth(cfg: AuthConfig) -> Any | None:
    """Return a FastMCP auth provider, or ``None`` for no inbound auth."""
    if cfg.mode == "none":
        return None
    if cfg.mode == "jwt":
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        kwargs: dict[str, Any] = {}
        if cfg.jwks_uri:
            kwargs["jwks_uri"] = cfg.jwks_uri
        if cfg.issuer:
            kwargs["issuer"] = cfg.issuer
        if cfg.audience:
            kwargs["audience"] = cfg.audience
        return JWTVerifier(**kwargs)
    raise ValueError(f"unknown auth mode: {cfg.mode!r}")
