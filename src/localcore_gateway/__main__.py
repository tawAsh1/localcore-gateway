"""``lcgw`` CLI: serve / dev / tools / invoke."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from localcore_gateway.config import load_config
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
        reload_dirs.add(cfg.resolved_code_root(tc.lambda_))

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


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    p = argparse.ArgumentParser(
        prog="lcgw",
        description="Local AWS Bedrock AgentCore "
        "Gateway (with a local Lambda backend).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_cfg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "-c", "--config", required=True, help="path to gateway config YAML"
        )

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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
