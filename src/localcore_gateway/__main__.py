"""``lcgw`` CLI: serve / dev / tools / invoke / sync / tail."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from localcore_gateway.config import GatewayConfig, load_config
from localcore_gateway.gateway import NAME_SEP, build_targets


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from localcore_gateway.app import build_app

    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    app, _mcp, targets = build_app(cfg)

    async def _run() -> None:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=cfg.server.host,
                port=cfg.server.port,
                log_level="info",
            )
        )
        url = f"http://{cfg.server.host}:{cfg.server.port}{cfg.server.path}"
        print(f"localcore-gateway MCP endpoint: {url}", file=sys.stderr)
        try:
            await server.serve()
        finally:
            for t in targets:
                await t.aclose()

    asyncio.run(_run())
    return 0


def _cmd_dev(args: argparse.Namespace) -> int:
    import uvicorn

    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port

    reload_dirs = {str(Path(args.config).resolve().parent)}
    for tc in cfg.targets:
        if tc.type == "lambda":  # only Lambda targets have local code roots to watch
            reload_dirs.update(cfg.resolved_code_roots(tc.lambda_))

    os.environ["LCGW_CONFIG"] = str(Path(args.config).resolve())
    url = f"http://{cfg.server.host}:{cfg.server.port}{cfg.server.path}"
    print(f"[dev] hot-reload MCP endpoint: {url}", file=sys.stderr)
    print(f"[dev] watching: {sorted(reload_dirs)}", file=sys.stderr)
    uvicorn.run(
        "localcore_gateway.app:asgi",
        factory=True,
        host=cfg.server.host,
        port=cfg.server.port,
        reload=True,
        reload_dirs=sorted(reload_dirs),
        log_level="info",
    )
    return 0


def _cmd_tools(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    targets = build_targets(cfg)
    out = [
        {
            "name": f"{t.name}{NAME_SEP}{td.name}",
            "description": td.description,
            "inputSchema": td.input_schema,
            # Only when declared (optional, mirrors MCP / AgentCore).
            **({"outputSchema": td.output_schema} if td.output_schema is not None else {}),
        }
        for t in targets
        for td in t.list_tools()
    ]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _cmd_invoke(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    targets = {t.name: t for t in build_targets(cfg)}

    sel = args.selector.replace("/", NAME_SEP)
    if NAME_SEP not in sel:
        print(f"selector must be 'target{NAME_SEP}tool'", file=sys.stderr)
        return 2
    tname, tool = sel.split(NAME_SEP, 1)
    if tname not in targets:
        print(f"unknown target {tname!r}; have {list(targets)}", file=sys.stderr)
        return 2

    arguments = json.loads(args.data) if args.data else {}

    async def _run() -> int:
        target = targets[tname]
        try:
            outcome = await target.call_tool(tool, arguments)
        finally:
            await target.aclose()
        for line in outcome.logs:
            print(line, file=sys.stderr)
        print(
            json.dumps(
                {"isError": outcome.is_error, "payload": outcome.payload},
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return 1 if outcome.is_error else 0

    return asyncio.run(_run())


def _admin_url(cfg: GatewayConfig, route: str) -> str:
    """Admin routes live at the server root, outside the MCP path."""
    return f"http://{cfg.server.host}:{cfg.server.port}{route}"


def _format_sync_result(name: str, res: object) -> tuple[str, bool]:
    """One summary block for a target's sync result; (text, is_error)."""
    if res == "static" or not isinstance(res, dict):
        return f"{name}: static (nothing to sync)", False
    if "error" in res:
        return f"{name}: ERROR {res['error']}", True
    line = f"{name}: +{len(res['added'])} added, -{len(res['removed'])} removed, ~{len(res['updated'])} updated"
    for mark, key in (("+", "added"), ("-", "removed"), ("~", "updated")):
        for tool in res[key]:
            # Raw (un-prefixed) names: aws-gateway targets expose verbatim
            # names, so prepending `<target>___` here would be wrong there.
            line += f"\n  {mark} {tool}"
    return line, False


def _cmd_sync(args: argparse.Namespace) -> int:
    import httpx

    cfg = load_config(args.config)
    body = {"target": args.target} if args.target else {}
    try:
        resp = httpx.post(_admin_url(cfg, "/-/sync"), json=body, timeout=60.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"sync failed: {exc} (is `lcgw serve` running?)", file=sys.stderr)
        return 1
    failed = False
    for name, res in resp.json()["targets"].items():
        line, is_error = _format_sync_result(name, res)
        print(line)
        failed = failed or is_error
    return 1 if failed else 0


def _ellipsize(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_invocation(rec: dict) -> str:
    """One tail line: time, status, tool, duration, compact args/result."""
    t = rec["time"][11:19]  # HH:MM:SS from the ISO timestamp
    status = "ERROR" if rec["is_error"] else "OK"
    line = (
        f"{t} {status:<5} {rec['tool']} ({rec['duration_ms']:.0f} ms) "
        f"args={_ellipsize(rec['arguments'], 60)} -> {_ellipsize(rec['payload'], 80)}"
    )
    if rec.get("contract_violation"):
        line += f" [contract: {_ellipsize(rec['contract_violation'], 60)}]"
    return line


def _cmd_tail(args: argparse.Namespace) -> int:
    import httpx

    cfg = load_config(args.config)
    url = _admin_url(cfg, "/-/invocations")

    def fetch(since: int, limit: int | None = None) -> dict:
        params: dict[str, int] = {"since": since}
        if limit is not None:
            params["limit"] = limit
        resp = httpx.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        return resp.json()

    def emit(rec: dict) -> None:
        print(json.dumps(rec, ensure_ascii=False) if args.json else _format_invocation(rec), flush=True)

    try:
        if args.lines:
            data = fetch(0)  # whole backlog, show the last N
            for rec in data["invocations"][-args.lines :]:
                emit(rec)
        else:
            data = fetch(0, limit=0)  # cursor-only bootstrap: tail from "now"
        since = data["next"]
        while True:
            time.sleep(0.5)
            data = fetch(since)
            for rec in data["invocations"]:
                emit(rec)
            since = data["next"]
    except KeyboardInterrupt:
        return 0
    except httpx.HTTPError as exc:
        print(f"tail failed: {exc} (is `lcgw serve` running?)", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    p = argparse.ArgumentParser(
        prog="lcgw",
        description="Local AWS Bedrock AgentCore Gateway (with a local Lambda backend).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_cfg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-c", "--config", required=True, help="path to gateway config YAML")

    sp = sub.add_parser("serve", help="serve the MCP gateway")
    add_cfg(sp)
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=_cmd_serve)

    sp = sub.add_parser("dev", help="serve with hot reload")
    add_cfg(sp)
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=_cmd_dev)

    sp = sub.add_parser("tools", help="print the aggregated tool catalog")
    add_cfg(sp)
    sp.set_defaults(func=_cmd_tools)

    sp = sub.add_parser("invoke", help="call one tool directly (no HTTP)")
    add_cfg(sp)
    sp.add_argument("selector", help="target___tool (or target/tool)")
    sp.add_argument("--data", help="JSON tool arguments", default="")
    sp.set_defaults(func=_cmd_invoke)

    sp = sub.add_parser("sync", help="re-sync targets on a running gateway (MCP targets re-discover)")
    add_cfg(sp)
    sp.add_argument("--target", help="sync only this target")
    sp.set_defaults(func=_cmd_sync)

    sp = sub.add_parser("tail", help="stream invocations from a running gateway")
    add_cfg(sp)
    sp.add_argument("-n", "--lines", type=int, default=0, help="show the last N invocations first")
    sp.add_argument("--json", action="store_true", help="emit raw JSONL instead of formatted lines")
    sp.set_defaults(func=_cmd_tail)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
