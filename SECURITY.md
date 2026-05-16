# Security Policy

`localcore-gateway` is an unofficial, community project (see `NOTICE`). It is a
**local development tool**, not a production gateway.

## Reporting a vulnerability

Please report security issues **privately** via GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(repository → **Security** → **Report a vulnerability**).

Do **not** open a public issue for security reports.

Please include: affected version/commit, reproduction steps, and impact.

## Scope & threat model (read this first)

This tool intentionally executes code with the privileges of the process that
runs it. The following are **known, by-design behaviors, not vulnerabilities**:

- The `native` Lambda backend imports and runs the configured handler **in the
  gateway process, unsandboxed**. Listing tools alone executes handler
  import-time code. Only point it at code you trust; use the `sam` backend for
  isolation.
- The MCP endpoint has **no inbound authentication** (by design — local dev
  tool). Bind to loopback only; if you must expose it, put it behind your own
  proxy/auth. Do not rely on the gateway to gate access.
- `lambda.env` in the config is plaintext; do not commit real secrets.

In scope: request-handling flaws, dependency vulnerabilities, supply-chain
integrity of the build.

## Supported versions

This project is pre-1.0; only the latest `main` is supported.
