"""Tiny stdlib HTTP client for the CLI to talk to the running daemon."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from . import config


def _resolve(instance: str) -> str:
    port = config.instance_port(instance)
    if port is None:
        raise SystemExit(
            f"unknown instance '{instance}'. Known: {sorted(config.load_instances())} "
            f"or start it with: cgw serve {instance}")
    return config.daemon_url(port)


def _get(base: str, path: str, timeout: float = 30):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.load(r)


def _post(base: str, path: str, data: dict, timeout: float = 30):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def cli_status(instance: str = config.DEFAULT_INSTANCE) -> int:
    base = _resolve(instance)
    try:
        print(json.dumps(_get(base, "/health"), indent=2, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"daemon not reachable at {base}: {e}\nStart it: cgw serve {instance}")
        return 1


def cli_ask(message: str, effort: str, timeout: int, cont: bool = False,
            instance: str = config.DEFAULT_INSTANCE, files: list[str] | None = None,
            chat: str | None = None) -> int:
    base = _resolve(instance)
    try:
        job = _post(base, "/ask", {"message": message, "effort": effort, "timeout": timeout,
                                   "continue": cont, "files": files, "chat": chat})
    except Exception as e:  # noqa: BLE001
        print(f"daemon not reachable at {base}: {e}\nStart it: cgw serve {instance}")
        return 1
    if "error" in job and "job_id" not in job:
        print(f"ERROR: {job['error']}", file=sys.stderr)
        return 2
    jid = job["job_id"]
    deadline = time.time() + timeout + 60
    last = None
    st: dict = {}
    while time.time() < deadline:
        time.sleep(2)
        try:
            st = _get(base, f"/jobs/{jid}")
        except Exception as e:  # noqa: BLE001
            print(f"poll error: {e}", file=sys.stderr)
            continue
        if st.get("progress") != last:
            last = st.get("progress")
            print(f"… {last}", file=sys.stderr, flush=True)
        if st.get("status") in ("done", "error"):
            break
    # Surface where this conversation lives so it can be resumed later.
    if st.get("conversation_url"):
        title = st.get("conversation_title") or ""
        print(f"… conversation: {st['conversation_url']}"
              f"{f'  ({title})' if title else ''}", file=sys.stderr, flush=True)
    if st.get("status") == "done":
        print(st.get("text", ""))
        return 0
    print(f"ERROR: {st.get('error', 'timeout')}", file=sys.stderr)
    return 2


def cli_chats(instance: str = config.DEFAULT_INSTANCE, limit: int = 30) -> int:
    """List recorded conversations so a user/agent can pick one to resume."""
    base = _resolve(instance)
    try:
        data = _get(base, "/conversations")
    except Exception:  # noqa: BLE001 — fall back to the on-disk store if daemon is down
        data = {"conversations": sorted(
            config.load_conversations().values(),
            key=lambda r: r.get("last_used_at", 0), reverse=True)}
    convs = data.get("conversations", [])[:limit]
    if not convs:
        print("no conversations recorded yet.")
        return 0
    for r in convs:
        title = (r.get("title") or "(untitled)")[:48]
        print(f"{r.get('last_used', '?'):19}  {r.get('turns', '?'):>3}t  "
              f"{title:50}  {r.get('url', '')}")
    return 0


def cli_instances() -> int:
    """List registered instances and probe each daemon's live health."""
    reg = config.load_instances()
    if not reg:
        print("no instances registered. Start one with: cgw serve <name>")
        return 0
    for name in sorted(reg):
        rec = reg[name]
        port = rec.get("port")
        base = config.daemon_url(int(port)) if port else "?"
        live = "down"
        try:
            h = _get(base, "/health", timeout=3)
            live = (f"up logged_in={h.get('logged_in')} busy={h.get('busy_count')} "
                    f"queued={h.get('queued')}")
        except Exception:  # noqa: BLE001
            pass
        print(f"{name:16} account={rec.get('account'):12} port={port}  {live}")
    return 0
