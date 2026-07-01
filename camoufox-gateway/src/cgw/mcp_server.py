"""stdio MCP server exposing the ChatGPT-Pro gateway as tools.

Thin client over the daemon's loopback HTTP API. Usable from Claude Code and codex.
Tools: chatgpt_status, chatgpt_ask, chatgpt_poll, chatgpt_login, chatgpt_instances.

Every tool takes an ``instance`` (named session) so several Pro conversations on
separate Camoufox profiles/daemons can be driven in parallel; it defaults to the
configured default instance.
"""

from __future__ import annotations

import asyncio

import aiohttp
from mcp.server.fastmcp import Context, FastMCP

from . import config

mcp = FastMCP("chatgpt-pro")


class _Unknown(Exception):
    """Requested instance is not registered / not running."""


def _base(instance: str) -> str:
    port = config.instance_port(instance)
    if port is None:
        raise _Unknown(
            f"unknown instance '{instance}'. Known: {sorted(config.load_instances())}. "
            f"Start it with:  cgw serve {instance}")
    return config.daemon_url(port)


async def _get(base: str, path: str):
    async with aiohttp.ClientSession() as s:
        async with s.get(base + path, timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json(), r.status


async def _post(base: str, path: str, data: dict):
    async with aiohttp.ClientSession() as s:
        async with s.post(base + path, json=data,
                          timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json(), r.status


def _down(instance: str, base: str) -> str:
    return (f"Gateway daemon for instance '{instance}' is not running at {base}.\n"
            "Start it on the workstation with:  cd ~/chatgpt-gateway/camoufox-gateway "
            f"&& uv run cgw serve {instance}   (or via the cgw-gateway@{instance} "
            "systemd service).")


@mcp.tool()
async def chatgpt_instances() -> str:
    """List the named ChatGPT-Pro instances (sessions) and whether each daemon is up."""
    reg = config.load_instances()
    if not reg:
        return ("No instances registered. Start one on the workstation with:  "
                "cgw serve <name>")
    out = []
    for name in sorted(reg):
        rec = reg[name]
        base = config.daemon_url(int(rec["port"])) if rec.get("port") else "?"
        try:
            h, _ = await _get(base, "/health")
            state = f"up  logged_in={h.get('logged_in')}  busy={h.get('busy_count')}  queued={h.get('queued')}"
        except Exception:
            state = "down"
        out.append(f"{name}  (account {rec.get('account')}, port {rec.get('port')}): {state}")
    return "\n".join(out)


@mcp.tool()
async def chatgpt_status(instance: str = config.DEFAULT_INSTANCE) -> str:
    """Check whether a ChatGPT-Pro instance is up and logged in. Call before asking."""
    try:
        base = _base(instance)
    except _Unknown as e:
        return str(e)
    try:
        data, _ = await _get(base, "/health")
    except Exception:
        return _down(instance, base)
    li = data.get("logged_in")
    lines = [
        f"Gateway: running (instance {data.get('instance')}, account {data.get('account')})",
        f"Logged in: {li}",
        f"Busy: {data.get('busy')}  Queued: {data.get('queued')}",
    ]
    if not li:
        lines.append(f"\nNot logged in. Run chatgpt_login, or `cgw login --headed {instance}` "
                     "on the workstation if a CAPTCHA appears.")
    return "\n".join(lines)


@mcp.tool()
async def chatgpt_ask(
    message: str,
    effort: str = "pro",
    timeout: int = 1200,
    system: str | None = None,
    instance: str = config.DEFAULT_INSTANCE,
    cont: bool = False,
    files: list[str] | None = None,
    ctx: Context | None = None,
) -> str:
    """Ask ChatGPT Pro and return its answer (markdown).

    Uses the logged-in browser session via a persistent Camoufox profile — no API key.
    effort: 'pro' (GPT-5.5 Pro deep reasoning, DEFAULT; can take minutes),
    'extended' (Very High thinking), 'high', 'standard', 'instant' (fast).
    instance: which named session to ask — run separate Pro conversations in parallel
    by pointing different calls at different instances (see chatgpt_instances).
    cont=True continues that instance's current conversation instead of starting fresh.
    files: local file paths (on the gateway host) to attach to the message — e.g. code
    files for ChatGPT to read/review. Each call otherwise starts a new conversation.
    Polls until complete; reports progress.
    """
    try:
        base = _base(instance)
    except _Unknown as e:
        return str(e)
    try:
        job, status = await _post(base, "/ask", {"message": message, "effort": effort,
                                                 "timeout": timeout, "system": system,
                                                 "continue": cont, "files": files})
    except Exception:
        return _down(instance, base)
    if status != 200:
        return f"Gateway error: {job.get('error', status)}"
    jid = job["job_id"]

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout + 60
    last = None
    while loop.time() < deadline:
        await asyncio.sleep(2)
        try:
            st, _ = await _get(base, f"/jobs/{jid}")
        except Exception:
            continue
        prog = st.get("progress")
        if ctx and prog and prog != last:
            last = prog
            try:
                await ctx.info(f"chatgpt: {prog}")
            except Exception:
                pass
        if st.get("status") == "done":
            return st.get("text", "")
        if st.get("status") == "error":
            return f"ChatGPT request failed: {st.get('error')}"
    return f"Timed out after {timeout}s waiting for ChatGPT (job {jid} still running)."


@mcp.tool()
async def chatgpt_poll(job_id: str, instance: str = config.DEFAULT_INSTANCE) -> str:
    """Poll a previously submitted job by id (status + partial/final text)."""
    try:
        base = _base(instance)
    except _Unknown as e:
        return str(e)
    try:
        st, code = await _get(base, f"/jobs/{job_id}")
    except Exception:
        return _down(instance, base)
    if code == 404:
        return f"Unknown job {job_id}"
    if st.get("status") == "done":
        return st.get("text", "")
    return f"status={st.get('status')} progress={st.get('progress')} error={st.get('error')}"


@mcp.tool()
async def chatgpt_login(instance: str = config.DEFAULT_INSTANCE) -> str:
    """Trigger a headless auto-login of an instance's ChatGPT profile."""
    try:
        base = _base(instance)
    except _Unknown as e:
        return str(e)
    try:
        data, code = await _post(base, "/login", {})
    except Exception:
        return _down(instance, base)
    if data.get("logged_in"):
        return "Logged in."
    return (f"Login not completed: {data.get('error', 'unknown')}. "
            f"If CAPTCHA, run `cgw login --headed {instance}` on the workstation.")


def run_mcp() -> None:
    mcp.run()
