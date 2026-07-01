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
            instance: str = config.DEFAULT_INSTANCE, files: list[str] | None = None) -> int:
    base = _resolve(instance)
    try:
        job = _post(base, "/ask", {"message": message, "effort": effort, "timeout": timeout,
                                   "continue": cont, "files": files})
    except Exception as e:  # noqa: BLE001
        print(f"daemon not reachable at {base}: {e}\nStart it: cgw serve {instance}")
        return 1
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
    if st.get("status") == "done":
        print(st.get("text", ""))
        return 0
    print(f"ERROR: {st.get('error', 'timeout')}", file=sys.stderr)
    return 2


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
