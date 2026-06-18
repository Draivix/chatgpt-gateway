"""Long-running gateway daemon: owns the Camoufox browser + a serialized job queue,
exposes a loopback HTTP API the MCP server / CLI poll.

One browser, one ChatGPT tab -> requests are serialized through a queue. Submitting
returns a job id immediately; clients poll /jobs/{id}. Extended-thinking answers can
take minutes, so nothing blocks on a single long HTTP request.
"""

from __future__ import annotations

import asyncio
import uuid

from aiohttp import web

from . import chat, config
from .browser import camoufox_kwargs, clear_stale_lock, first_page
from .login import RateLimited, ensure_logged_in, is_logged_in


def log(msg: str) -> None:
    import time
    print(f"[daemon {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Gateway:
    def __init__(self, account: str, headed: bool, with_addon: bool):
        self.account = account
        self.headed = headed
        self.with_addon = with_addon
        self.page = None
        self.jobs: dict[str, dict] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.logged_in = False
        self.busy: str | None = None
        self.login_lock = asyncio.Lock()

    async def ensure_login(self) -> bool:
        async with self.login_lock:
            if await is_logged_in(self.page):
                self.logged_in = True
                return True
            log("session invalid; attempting auto-login")
            try:
                self.logged_in = await ensure_logged_in(self.page, config.load_account(self.account))
            except RateLimited as e:
                self.logged_in = False
                raise web.HTTPServiceUnavailable(text=str(e))
            return self.logged_in

    async def worker(self):
        while True:
            jid = await self.queue.get()
            job = self.jobs.get(jid)
            if job is None:
                self.queue.task_done()
                continue
            job["status"] = "running"
            self.busy = jid

            def prog(m, _job=job):
                _job["progress"] = m

            try:
                if not await is_logged_in(self.page):
                    await self.ensure_login()
                res = await chat.ask(
                    self.page, job["message"], effort=job["effort"],
                    system=job.get("system"), timeout_s=job["timeout"], progress=prog,
                )
                if res.get("ok"):
                    job.update(status="done", text=res["text"],
                               model=res.get("model"), elapsed=res.get("elapsed"))
                else:
                    job.update(status="error", error=res.get("error", "unknown"))
            except web.HTTPException as e:
                job.update(status="error", error=e.text or "login required")
            except Exception as e:  # noqa: BLE001
                job.update(status="error", error=f"{type(e).__name__}: {e}")
            finally:
                self.busy = None
                self.queue.task_done()
                # keep memory bounded
                if len(self.jobs) > 200:
                    for k in list(self.jobs)[:-100]:
                        self.jobs.pop(k, None)


async def _h_health(request: web.Request) -> web.Response:
    gw: Gateway = request.app["gw"]
    return web.json_response({
        "ok": True,
        "account": gw.account,
        "logged_in": await is_logged_in(gw.page),
        "busy": gw.busy,
        "queued": gw.queue.qsize(),
        "jobs": len(gw.jobs),
    })


async def _h_ask(request: web.Request) -> web.Response:
    gw: Gateway = request.app["gw"]
    data = await request.json()
    msg = (data.get("message") or "").strip()
    if not msg:
        return web.json_response({"error": "message required"}, status=400)
    jid = uuid.uuid4().hex[:12]
    gw.jobs[jid] = {
        "status": "queued", "progress": "queued",
        "message": msg,
        "effort": data.get("effort", "pro"),
        "system": data.get("system"),
        "timeout": int(data.get("timeout") or config.ASK_TIMEOUT_S),
    }
    await gw.queue.put(jid)
    return web.json_response({"job_id": jid, "queued": gw.queue.qsize()})


async def _h_job(request: web.Request) -> web.Response:
    gw: Gateway = request.app["gw"]
    job = gw.jobs.get(request.match_info["jid"])
    if not job:
        return web.json_response({"error": "unknown job"}, status=404)
    out = {k: job.get(k) for k in ("status", "progress", "text", "error", "model", "elapsed")}
    return web.json_response(out)


async def _h_login(request: web.Request) -> web.Response:
    gw: Gateway = request.app["gw"]
    try:
        ok = await gw.ensure_login()
    except web.HTTPException as e:
        return web.json_response({"logged_in": False, "error": e.text}, status=503)
    return web.json_response({"logged_in": ok})


async def _serve(account: str, headed: bool, with_addon: bool):
    from camoufox.async_api import AsyncCamoufox

    gw = Gateway(account, headed, with_addon)
    clear_stale_lock(account)
    kw = camoufox_kwargs(account, headless=not headed, with_addon=with_addon)
    log(f"launching Camoufox account={account} headless={not headed} addon={with_addon}")
    async with AsyncCamoufox(**kw) as ctx:
        gw.page = await first_page(ctx)
        try:
            await gw.ensure_login()
            log(f"logged_in={gw.logged_in}")
        except web.HTTPException as e:
            log(f"login deferred: {e.text}")

        worker = asyncio.create_task(gw.worker())
        app = web.Application()
        app["gw"] = gw
        app.add_routes([
            web.get("/health", _h_health),
            web.post("/ask", _h_ask),
            web.get("/jobs/{jid}", _h_job),
            web.post("/login", _h_login),
        ])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.DAEMON_HOST, config.DAEMON_PORT)
        await site.start()
        log(f"HTTP API on {config.DAEMON_URL}")
        try:
            await asyncio.Event().wait()  # run until killed
        finally:
            worker.cancel()
            await runner.cleanup()


def run_daemon(account: str, *, headed: bool, with_addon: bool) -> int:
    try:
        asyncio.run(_serve(account, headed, with_addon))
    except KeyboardInterrupt:
        pass
    return 0
