"""cgw command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import config


async def _login(account: str, headed: bool, debug: bool, with_addon: bool, hold: int) -> bool:
    from camoufox.async_api import AsyncCamoufox

    from .browser import camoufox_kwargs, clear_stale_lock, first_page
    from .login import ensure_logged_in, is_logged_in

    acct = config.load_account(account)
    clear_stale_lock(account)
    kw = camoufox_kwargs(account, headless=not headed, with_addon=with_addon)
    print(f"[cgw] launching Camoufox headed={headed} account={account} ({acct['email']})")
    async with AsyncCamoufox(**kw) as ctx:
        page = await first_page(ctx)
        ok = await ensure_logged_in(page, acct, debug=debug)
        if not ok and headed and hold > 0:
            print(f"[cgw] auto-login incomplete — window open {hold}s for manual finish "
                  "(solve any CAPTCHA, then it will detect login)")
            for _ in range(hold):
                await asyncio.sleep(1)
                if await is_logged_in(page):
                    ok = True
                    break
        print(f"[cgw] logged_in={ok}; session persisted to {config.profile_dir(account)}")
        return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cgw", description="Camoufox ChatGPT-Pro gateway")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="log a persistent profile into chatgpt.com")
    p.add_argument("account", nargs="?", default=config.DEFAULT_ACCOUNT)
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--debug", action="store_true", help="dump screenshots/HTML per step")
    p.add_argument("--no-addon", action="store_true", help="do not load the extension")
    p.add_argument("--hold", type=int, default=180,
                   help="headed: seconds to keep window open for manual finish")

    sub.add_parser("accounts", help="list available accounts")

    p = sub.add_parser("serve", help="run the gateway daemon")
    p.add_argument("account", nargs="?", default=config.DEFAULT_ACCOUNT)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-addon", action="store_true")

    p = sub.add_parser("ask", help="one-shot ask via the running daemon")
    p.add_argument("message")
    p.add_argument("--effort", default="pro",
                   choices=["instant", "standard", "high", "extended",
                            "pro", "pro-standard", "pro-extended"])
    p.add_argument("--timeout", type=int, default=config.ASK_TIMEOUT_S)

    sub.add_parser("status", help="check the running daemon")
    sub.add_parser("mcp", help="run the stdio MCP server")

    p = sub.add_parser("probe", help="dev: capture live ChatGPT DOM + network")
    p.add_argument("account", nargs="?", default=config.DEFAULT_ACCOUNT)
    p.add_argument("--headed", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "accounts":
        print("\n".join(config.list_accounts()))
        return 0
    if args.cmd == "login":
        ok = asyncio.run(_login(args.account, args.headed, args.debug,
                                not args.no_addon, args.hold))
        return 0 if ok else 2
    if args.cmd == "serve":
        from .daemon import run_daemon
        return run_daemon(args.account, headed=args.headed, with_addon=not args.no_addon)
    if args.cmd == "ask":
        from .client import cli_ask
        return cli_ask(args.message, args.effort, args.timeout)
    if args.cmd == "status":
        from .client import cli_status
        return cli_status()
    if args.cmd == "mcp":
        from .mcp_server import run_mcp
        run_mcp()
        return 0
    if args.cmd == "probe":
        from .probe import run_probe
        return asyncio.run(run_probe(args.account, args.headed))
    return 1


if __name__ == "__main__":
    sys.exit(main())
