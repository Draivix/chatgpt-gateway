"""Tiny stdlib HTTP client for the CLI to talk to the running daemon."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from . import config


def _get(path: str, timeout: float = 30):
    with urllib.request.urlopen(config.DAEMON_URL + path, timeout=timeout) as r:
        return json.load(r)


def _post(path: str, data: dict, timeout: float = 30):
    req = urllib.request.Request(
        config.DAEMON_URL + path,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def cli_status() -> int:
    try:
        print(json.dumps(_get("/health"), indent=2, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"daemon not reachable at {config.DAEMON_URL}: {e}\nStart it: cgw serve")
        return 1


def cli_ask(message: str, effort: str, timeout: int) -> int:
    try:
        job = _post("/ask", {"message": message, "effort": effort, "timeout": timeout})
    except Exception as e:  # noqa: BLE001
        print(f"daemon not reachable at {config.DAEMON_URL}: {e}\nStart it: cgw serve")
        return 1
    jid = job["job_id"]
    deadline = time.time() + timeout + 60
    last = None
    st: dict = {}
    while time.time() < deadline:
        time.sleep(2)
        try:
            st = _get(f"/jobs/{jid}")
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
