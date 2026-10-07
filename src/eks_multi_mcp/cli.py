"""Command line: `serve` (default) runs the MCP server; `targets` and `doctor` inspect
resolution from a terminal without an MCP client."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import load_settings


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="eks-multi-mcp", description=__doc__)
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "targets", "doctor"])
    ap.add_argument("--config", help="cluster map / settings YAML (default: ~/.config/eks-multi-mcp/config.yaml)")
    ap.add_argument("--allow-write", action="store_true", help="enable create/apply/patch/delete tools")
    ap.add_argument("--allow-sensitive-data-access", action="store_true",
                    help="enable pod logs, CloudWatch logs and unredacted Secrets")
    ap.add_argument("--kubeconfig", action="append", help="kubeconfig path (repeatable; default $KUBECONFIG)")
    ap.add_argument("--no-kubeconfig", action="store_true", help="use only the cluster map / AWS discovery")
    ap.add_argument("--selector", default=None, help="targets/doctor: target selector, e.g. env:prod")
    ap.add_argument("--no-access-check", action="store_true", help="doctor: skip AWS/Kubernetes calls")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http", "sse"],
                    help="HTTP transports are unauthenticated, so they refuse writes and sensitive access")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr)
    settings = load_settings(args.config)
    settings.log_level = args.log_level.upper()
    settings.allow_write = settings.allow_write or args.allow_write
    settings.allow_sensitive_data_access = settings.allow_sensitive_data_access or args.allow_sensitive_data_access
    if args.transport != "stdio" and (settings.allow_write or settings.allow_sensitive_data_access):
        # The HTTP transports have no authentication: any local process can connect, act as
        # the MCP client and answer its own approval prompts.
        raise SystemExit(f"eks-multi-mcp: writes and sensitive data access are available only over stdio; "
                         f"the {args.transport} transport has no client authentication. Drop --allow-write "
                         "and --allow-sensitive-data-access (and their config settings), or use stdio.")
    if args.kubeconfig:
        settings.kubeconfig_paths = args.kubeconfig
    if args.no_kubeconfig:
        settings.use_kubeconfig = False

    from .server import EksMultiServer

    srv = EksMultiServer(settings)

    if settings.aws_discovery:
        _call(srv, "discover_clusters", {})

    if args.command == "serve":
        srv.mcp.run(transport=args.transport)
        return
    if args.command == "targets":
        res = _call(srv, "list_targets", {"selector": args.selector})
        rows = res["targets"]
        cols = ["target", "env", "account_name", "account_id", "region", "read_profile", "write_profile", "warnings"]
        widths = {c: max(len(c), *(len(str(r[c] or "")) for r in rows)) if rows else len(c) for c in cols}
        print("  ".join(c.upper().ljust(widths[c]) for c in cols))
        for r in rows:
            print("  ".join(str(r[c] if r[c] is not None else "-").ljust(widths[c]) for c in cols))
        print(f"\n{res['count']} targets", file=sys.stderr)
        return
    if args.command == "doctor":
        res = _call(srv, "doctor", {"selector": args.selector, "check_access": not args.no_access_check})
        print(json.dumps(res, indent=2, default=str))
        sys.exit(1 if res["targets_with_problems"] else 0)


def _call(srv, name: str, arguments: dict):
    async def run():
        res = await srv.mcp.call_tool(name, arguments)
        if getattr(res, "is_error", False):
            raise SystemExit(res.content[0].text)
        sc = res.structured_content
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc if sc is not None else json.loads(res.content[0].text)

    return asyncio.run(run())


if __name__ == "__main__":
    main()
