"""cgw command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import config


async def _login(instance: str, account: str, headed: bool, debug: bool,
                 with_addon: bool, hold: int) -> bool:
    from camoufox.async_api import AsyncCamoufox

    from .browser import camoufox_kwargs, clear_stale_lock, first_page
    from .login import ensure_logged_in, is_logged_in

    acct = config.load_account(account)
    clear_stale_lock(instance)
    kw = camoufox_kwargs(instance, headless=not headed, with_addon=with_addon)
    print(f"[cgw] launching Camoufox headed={headed} instance={instance} "
          f"account={account} ({acct['email']})")
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
        print(f"[cgw] logged_in={ok}; session persisted to {config.profile_dir(instance)}")
        return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cgw", description="Camoufox ChatGPT-Pro gateway")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="log a persistent profile (instance) into chatgpt.com")
    p.add_argument("instance", nargs="?", default=config.DEFAULT_INSTANCE,
                   help="named session/profile to log in (default from CGW_INSTANCE)")
    p.add_argument("--account", default=None,
                   help="accounts.json key for creds (default: same-named account, else default)")
    p.add_argument("--headed", dest="headed", action="store_true", default=config.HEADED,
                   help="show the browser window (default from CGW_HEADED)")
    p.add_argument("--headless", dest="headed", action="store_false",
                   help="force no window, overriding CGW_HEADED")
    p.add_argument("--debug", action="store_true", help="dump screenshots/HTML per step")
    p.add_argument("--no-addon", action="store_true", help="do not load the extension")
    p.add_argument("--hold", type=int, default=180,
                   help="headed: seconds to keep window open for manual finish")

    sub.add_parser("accounts", help="list available accounts")
    sub.add_parser("instances", help="list named instances (sessions) and their daemons")

    p = sub.add_parser("serve", help="run the gateway daemon for one named instance")
    p.add_argument("instance", nargs="?", default=config.DEFAULT_INSTANCE,
                   help="named session/profile to serve (default from CGW_INSTANCE)")
    p.add_argument("--account", default=None,
                   help="accounts.json key for creds (default: same-named account, else default)")
    p.add_argument("--port", type=int, default=None,
                   help="bind port (default: reuse/allocate from the instance registry)")
    p.add_argument("--headed", dest="headed", action="store_true", default=config.HEADED,
                   help="show the browser window (default from CGW_HEADED)")
    p.add_argument("--headless", dest="headed", action="store_false",
                   help="force no window, overriding CGW_HEADED")
    p.add_argument("--no-addon", action="store_true")

    p = sub.add_parser("ask", help="one-shot ask via the running daemon")
    p.add_argument("message", nargs="?", default="",
                   help="the prompt. Optional only with --chat (fetch that chat's "
                        "latest answer without sending anything).")
    p.add_argument("--instance", default=config.DEFAULT_INSTANCE,
                   help="which named instance to ask (default from CGW_INSTANCE)")
    p.add_argument("--effort", default="pro",
                   choices=["instant", "standard", "high", "extended",
                            "pro", "pro-standard", "pro-extended"])
    p.add_argument("--timeout", type=int, default=config.ASK_TIMEOUT_S)
    p.add_argument("--continue", dest="cont", action="store_true",
                   help="continue the current conversation instead of starting a new chat")
    p.add_argument("--chat", dest="chat", default=None, metavar="URL_OR_ID",
                   help="RESUME a specific past conversation by URL or id (see 'cgw "
                        "chats'). With no message, just fetch its latest answer.")
    p.add_argument("--file", dest="files", action="append", metavar="PATH",
                   help="attach a local file to the message (repeatable). Paths are on "
                        "the gateway host.")

    p = sub.add_parser("chats", help="list recorded conversations you can resume")
    p.add_argument("--instance", default=config.DEFAULT_INSTANCE,
                   help="which named instance to query (default from CGW_INSTANCE)")
    p.add_argument("--limit", type=int, default=30, help="max rows to show")

    p = sub.add_parser("status", help="check the running daemon")
    p.add_argument("--instance", default=config.DEFAULT_INSTANCE,
                   help="which named instance to check (default from CGW_INSTANCE)")
    sub.add_parser("mcp", help="run the stdio MCP server")

    p = sub.add_parser("probe", help="dev: capture live ChatGPT DOM + network")
    p.add_argument("instance", nargs="?", default=config.DEFAULT_INSTANCE)
    p.add_argument("--account", default=None,
                   help="accounts.json key for creds (default: same-named account, else default)")
    p.add_argument("--headed", dest="headed", action="store_true", default=config.HEADED)
    p.add_argument("--headless", dest="headed", action="store_false")

    args = ap.parse_args(argv)

    if args.cmd == "accounts":
        print("\n".join(config.list_accounts()))
        return 0
    if args.cmd == "instances":
        from .client import cli_instances
        return cli_instances()
    if args.cmd == "login":
        account = config.resolve_account(args.instance, args.account)
        ok = asyncio.run(_login(args.instance, account, args.headed, args.debug,
                                not args.no_addon, args.hold))
        return 0 if ok else 2
    if args.cmd == "serve":
        from .daemon import run_daemon
        account = config.resolve_account(args.instance, args.account)
        explicit = args.port if args.port is not None else (
            config.DAEMON_PORT if config._PORT_FORCED else None)
        port = config.allocate_port(args.instance, account, explicit)
        return run_daemon(args.instance, account, port,
                          headed=args.headed, with_addon=not args.no_addon)
    if args.cmd == "ask":
        from .client import cli_ask
        if not args.message and not args.chat:
            ap.error("message is required unless --chat is given")
        return cli_ask(args.message, args.effort, args.timeout, cont=args.cont,
                       instance=args.instance, files=args.files, chat=args.chat)
    if args.cmd == "chats":
        from .client import cli_chats
        return cli_chats(args.instance, args.limit)
    if args.cmd == "status":
        from .client import cli_status
        return cli_status(args.instance)
    if args.cmd == "mcp":
        from .mcp_server import run_mcp
        run_mcp()
        return 0
    if args.cmd == "probe":
        from .probe import run_probe
        account = config.resolve_account(args.instance, args.account)
        return asyncio.run(run_probe(args.instance, account, args.headed))
    return 1


if __name__ == "__main__":
    sys.exit(main())
