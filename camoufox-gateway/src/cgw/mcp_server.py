"""stdio MCP server exposing the ChatGPT-Pro gateway as tools.

Thin client over the daemon's loopback HTTP API. Usable from Claude Code and codex.
Tools: chatgpt_status, chatgpt_ask, chatgpt_poll, chatgpt_login.
"""

from __future__ import annotations

import asyncio

import aiohttp
from mcp.server.fastmcp import Context, FastMCP

from . import config

mcp = FastMCP("chatgpt-pro")


async def _get(path: str):
    async with aiohttp.ClientSession() as s:
        async with s.get(config.DAEMON_URL + path, timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json(), r.status


async def _post(path: str, data: dict):
    async with aiohttp.ClientSession() as s:
        async with s.post(config.DAEMON_URL + path, json=data,
                          timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json(), r.status


_DOWN = (f"Gateway daemon is not running at {config.DAEMON_URL}.\n"
         "Start it on the workstation with:  cd ~/chatgpt-gateway/camoufox-gateway "
         "&& uv run cgw serve   (or via the systemd user service).")


@mcp.tool()
async def chatgpt_status() -> str:
    """Check whether the ChatGPT-Pro gateway is up and logged in. Call before asking."""
    try:
        data, _ = await _get("/health")
    except Exception:
        return _DOWN
    li = data.get("logged_in")
    lines = [
        f"Gateway: running (account {data.get('account')})",
        f"Logged in: {li}",
        f"Busy: {data.get('busy')}  Queued: {data.get('queued')}",
    ]
    if not li:
        lines.append("\nNot logged in. Run the chatgpt_login tool, or `cgw login --headed` "
                     "on the workstation if a CAPTCHA appears.")
    return "\n".join(lines)


@mcp.tool()
async def chatgpt_ask(
    message: str,
    effort: str = "pro",
    timeout: int = 1200,
    system: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Ask ChatGPT Pro and return its answer (markdown).

    Uses the logged-in browser session via a persistent Camoufox profile — no API key.
    effort: 'pro' (GPT-5.5 Pro deep reasoning, DEFAULT; can take minutes),
    'extended' (Very High thinking), 'high', 'standard', 'instant' (fast).
    Each call starts a fresh conversation. Polls until complete; reports progress.
    """
    try:
        job, status = await _post("/ask", {"message": message, "effort": effort,
                                           "timeout": timeout, "system": system})
    except Exception:
        return _DOWN
    if status != 200:
        return f"Gateway error: {job.get('error', status)}"
    jid = job["job_id"]

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout + 60
    last = None
    while loop.time() < deadline:
        await asyncio.sleep(2)
        try:
            st, _ = await _get(f"/jobs/{jid}")
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
async def chatgpt_poll(job_id: str) -> str:
    """Poll a previously submitted job by id (status + partial/final text)."""
    try:
        st, code = await _get(f"/jobs/{job_id}")
    except Exception:
        return _DOWN
    if code == 404:
        return f"Unknown job {job_id}"
    if st.get("status") == "done":
        return st.get("text", "")
    return f"status={st.get('status')} progress={st.get('progress')} error={st.get('error')}"


@mcp.tool()
async def chatgpt_login() -> str:
    """Trigger a headless auto-login of the gateway's ChatGPT profile."""
    try:
        data, code = await _post("/login", {})
    except Exception:
        return _DOWN
    if data.get("logged_in"):
        return "Logged in."
    return (f"Login not completed: {data.get('error', 'unknown')}. "
            "If CAPTCHA, run `cgw login --headed` on the workstation.")


def run_mcp() -> None:
    mcp.run()
