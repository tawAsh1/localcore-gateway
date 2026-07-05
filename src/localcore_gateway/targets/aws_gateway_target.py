"""AWS-gateway passthrough target: a REAL deployed AgentCore Gateway, proxied.

No AWS analog as a target type -- this exists purely for the hybrid debugging
workflow: run the one target you're developing locally (lambda / openapi /
mcp) while every other tool of your production toolset passes through to the
deployed gateway, all behind one local MCP endpoint. A deployed gateway is
itself an MCP server over streamable HTTP, so all the MCP-passthrough
machinery is reused as-is (eager discovery, persistent session with re-open,
resync); what differs is naming and auth:

* The remote tools already carry AgentCore's ``remoteTarget___tool`` names
  and are exposed **verbatim** -- ``prefix_tools = False``, no local
  ``<name>___`` prefix (re-prefixing would double it). ``name`` is for
  identification/logging only; a collision with another target's tool is a
  config error (build) or that target's error (resync).
* Outbound auth is ``bearer`` (OAuth/JWT-configured gateways) or ``sigv4``
  (IAM): each request is SigV4-signed for the ``bedrock-agentcore`` service
  via botocore. sigv4 needs the ``aws`` extra (boto3).
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp.client.transports import ClientTransport, StreamableHttpTransport

from localcore_gateway.aws_deps import require_boto3
from localcore_gateway.config import AWSGatewayAuthConfig
from localcore_gateway.targets.mcp_target import MCPTarget
from localcore_gateway.targets.openapi_target import _ApiKeyAuth


class _SigV4Auth(httpx.Auth):
    """SigV4-signs every request (IAM auth).

    ``service`` is the signing scope: ``bedrock-agentcore`` for aws-gateway
    passthrough, the actual AWS service (``lambda``, ``s3``, ...) for Smithy
    targets. ``requires_request_body``: the signature covers the payload
    hash, so httpx must materialize ``request.content`` (and Content-Length)
    before the auth flow runs.
    """

    requires_request_body = True

    def __init__(
        self,
        auth: Any,  # AWSGatewayAuthConfig / SmithyAuthConfig (duck-typed: region/profile)
        service: str = "bedrock-agentcore",
        kind: str = "aws-gateway",
    ) -> None:
        boto3 = require_boto3()
        from botocore.auth import SigV4Auth

        session = boto3.Session(profile_name=auth.profile, region_name=auth.region)
        region = session.region_name
        if not region:
            raise ValueError(f"{kind} target: auth.region is required (no region found in the AWS profile chain)")
        creds = session.get_credentials()
        if creds is None:
            raise ValueError(f"{kind} target: no AWS credentials found (set auth.profile or the default chain)")
        self._signer = SigV4Auth(creds, service, region)

    def auth_flow(self, request: httpx.Request):
        from botocore.awsrequest import AWSRequest

        aws_req = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={"Content-Type": request.headers.get("content-type", "application/json")},
        )
        self._signer.add_auth(aws_req)
        # Copy what signing added (Authorization, X-Amz-Date, and the session
        # token when present) onto the real request. `host` is signed from
        # the URL and sent by httpx itself, consistently.
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        yield request


def _build_gateway_auth(a: AWSGatewayAuthConfig) -> httpx.Auth | None:
    if a.type == "none":
        return None
    if a.type == "bearer":
        return _ApiKeyAuth("header", "Authorization", f"Bearer {a.value}")
    return _SigV4Auth(a)


class AWSGatewayTarget(MCPTarget):
    """MCPTarget pointed at a real gateway: verbatim names, AWS auth."""

    _kind = "aws-gateway"
    prefix_tools = False  # remote names are already `remoteTarget___tool`

    def _transport(self, *, keep_alive: bool = True) -> ClientTransport:  # noqa: ARG002  # http has no keep_alive
        cfg: Any = self._cfg  # AWSGatewayTargetConfig (duck-compatible with the base)
        return StreamableHttpTransport(cfg.url, headers=dict(cfg.headers), auth=_build_gateway_auth(cfg.auth))
