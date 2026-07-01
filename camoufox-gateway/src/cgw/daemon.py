"""Long-running gateway daemon: owns the Camoufox browser + a POOL of ChatGPT tabs,
exposes a loopback HTTP API the MCP server / CLI poll.

One browser, N tabs (config.WORKERS). Each tab is an independent ChatGPT conversation
on the same logged-in account, so several agents can run concurrently instead of all
queueing behind one minutes-long Pro answer. Submitting returns a job id immediately;
clients poll /jobs/{id}. ``--continue`` jobs are pinned to worker 0's tab so a single
interactive thread keeps its context; one-shot jobs fan out across all tabs.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from aiohttp import web

from . import chat, config
from .browser import camoufox_kwargs, clear_stale_lock, first_page
from .login import RateLimited, ensure_logged_in, is_logged_in


def log(msg: str) -> None:
    import time
    print(f"[daemon {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Gateway:
    def __init__(self, instance: str, account: str, headed: bool, with_addon: bool):
        self.instance = instance
        self.account = account
        self.headed = headed
        self.with_addon = with_addon
        self.pages: list = []                       # one tab per worker
        self.jobs: dict[str, dict] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()       # one-shot jobs (any worker)
        self.cont_queue: asyncio.Queue[str] = asyncio.Queue()  # --continue jobs (worker 0 only)
        self.logged_in = False
        self.busy: dict[int, str | None] = {}       # worker id -> job id (or None)
        self.login_lock = asyncio.Lock()

    @property
    def page(self):
        """Primary tab (worker 0) — kept for health checks / single-tab callers."""
        return self.pages[0] if self.pages else None

    async def ensure_login(self, page=None) -> bool:
        page = page or self.page
        async with self.login_lock:
            if await is_logged_in(page):
                self.logged_in = True
                return True
            log("session invalid; attempting auto-login")
            try:
                self.logged_in = await ensure_logged_in(page, config.load_account(self.account))
            except RateLimited as e:
                self.logged_in = False
                raise web.HTTPServiceUnavailable(text=str(e))
            return self.logged_in

    async def _next_job(self, wid: int) -> tuple[asyncio.Queue, str]:
        """Pick the next job for this worker.

        Worker 0 owns the continue queue (thread affinity) and also helps with
        one-shot jobs; it polls both. Other workers block on the one-shot queue.
        """
        if wid != 0:
            jid = await self.queue.get()
            return self.queue, jid
        while True:
            if not self.cont_queue.empty():
                return self.cont_queue, self.cont_queue.get_nowait()
            if not self.queue.empty():
                return self.queue, self.queue.get_nowait()
            await asyncio.sleep(0.15)

    async def worker(self, wid: int):
        page = self.pages[wid]
        self.busy[wid] = None
        while True:
            q, jid = await self._next_job(wid)
            job = self.jobs.get(jid)
            if job is None:
                q.task_done()
                continue
            job["status"] = "running"
            job["worker"] = wid
            self.busy[wid] = jid

            def prog(m, _job=job):
                _job["progress"] = m

            try:
                if not await is_logged_in(page):
                    await self.ensure_login(page)
                # Hard watchdog: never let a stuck tab pin a worker forever.
                res = await asyncio.wait_for(
                    chat.ask(
                        page, job["message"], effort=job["effort"],
                        system=job.get("system"), timeout_s=job["timeout"], progress=prog,
                        cont=job.get("cont", False), files=job.get("files"),
                    ),
                    timeout=job["timeout"] + 90,
                )
                if res.get("ok"):
                    job.update(status="done", text=res["text"],
                               model=res.get("model"), elapsed=res.get("elapsed"))
                else:
                    job.update(status="error", error=res.get("error", "unknown"))
            except asyncio.TimeoutError:
                job.update(status="error", error="watchdog timeout")
                await self._recover(page)
            except web.HTTPException as e:
                job.update(status="error", error=e.text or "login required")
            except Exception as e:  # noqa: BLE001
                job.update(status="error", error=f"{type(e).__name__}: {e}")
                await self._recover(page)
            finally:
                self.busy[wid] = None
                q.task_done()
                if len(self.jobs) > 200:
                    for k in list(self.jobs)[:-100]:
                        self.jobs.pop(k, None)

    async def _recover(self, page) -> None:
        """Return a tab to a clean, ready-to-use state after a failure."""
        with contextlib.suppress(Exception):
            await chat.new_chat(page)


async def _h_health(request: web.Request) -> web.Response:
    gw: Gateway = request.app["gw"]
    busy = [j for j in gw.busy.values() if j]
    return web.json_response({
        "ok": True,
        "instance": gw.instance,
        "account": gw.account,
        "logged_in": await is_logged_in(gw.page),
        "workers": len(gw.pages),
        "busy": busy,
        "busy_count": len(busy),
        "queued": gw.queue.qsize() + gw.cont_queue.qsize(),
        "jobs": len(gw.jobs),
    })


async def _h_ask(request: web.Request) -> web.Response:
    gw: Gateway = request.app["gw"]
    data = await request.json()
    msg = (data.get("message") or "").strip()
    if not msg:
        return web.json_response({"error": "message required"}, status=400)
    jid = uuid.uuid4().hex[:12]
    cont = bool(data.get("continue"))
    files = data.get("files") or None
    if files is not None and not isinstance(files, list):
        return web.json_response({"error": "files must be a list of paths"}, status=400)
    gw.jobs[jid] = {
        "status": "queued", "progress": "queued",
        "message": msg,
        "effort": data.get("effort", "pro"),
        "system": data.get("system"),
        "timeout": int(data.get("timeout") or config.ASK_TIMEOUT_S),
        "cont": cont,
        "files": files,
    }
    await (gw.cont_queue if cont else gw.queue).put(jid)
    return web.json_response(
        {"job_id": jid, "queued": gw.queue.qsize() + gw.cont_queue.qsize()})


async def _h_job(request: web.Request) -> web.Response:
    gw: Gateway = request.app["gw"]
    job = gw.jobs.get(request.match_info["jid"])
    if not job:
        return web.json_response({"error": "unknown job"}, status=404)
    out = {k: job.get(k) for k in ("status", "progress", "text", "error", "model", "elapsed", "worker")}
    return web.json_response(out)


async def _h_login(request: web.Request) -> web.Response:
    gw: Gateway = request.app["gw"]
    try:
        ok = await gw.ensure_login()
    except web.HTTPException as e:
        return web.json_response({"logged_in": False, "error": e.text}, status=503)
    return web.json_response({"logged_in": ok})


async def _serve(instance: str, account: str, port: int, headed: bool, with_addon: bool):
    from camoufox.async_api import AsyncCamoufox

    gw = Gateway(instance, account, headed, with_addon)
    clear_stale_lock(instance)
    kw = camoufox_kwargs(instance, headless=not headed, with_addon=with_addon)
    n = config.WORKERS
    log(f"launching Camoufox instance={instance} account={account} port={port} "
        f"headless={not headed} addon={with_addon} workers={n}")
    async with AsyncCamoufox(**kw) as ctx:
        # Open N pages sharing the one logged-in session (Firefox: each is a window).
        gw.pages = [await first_page(ctx)]
        for _ in range(1, n):
            gw.pages.append(await ctx.new_page())
        # chatgpt.com cold-loads slowly under a headed browser; give navigation room.
        for p in gw.pages:
            with contextlib.suppress(Exception):
                p.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
                p.set_default_timeout(config.ACTION_TIMEOUT_MS)
        # Login + tab-warm are best-effort at startup and MUST NOT crash the daemon:
        # each worker re-checks login (ensure_login) and navigates (new_chat) on its
        # first job, so a slow/expired session just defers, it does not kill the pool.
        try:
            await asyncio.wait_for(gw.ensure_login(gw.pages[0]), timeout=90)
            log(f"logged_in={gw.logged_in}")
        except Exception as e:  # noqa: BLE001
            log(f"startup login deferred to first job: {type(e).__name__}: {e}")
        for p in gw.pages[1:]:
            with contextlib.suppress(Exception):
                await p.goto(config.CHATGPT_URL, wait_until="commit", timeout=20_000)

        workers = [asyncio.create_task(gw.worker(wid)) for wid in range(n)]
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
        site = web.TCPSite(runner, config.DAEMON_HOST, port)
        await site.start()
        log(f"HTTP API on {config.daemon_url(port)}")
        try:
            await asyncio.Event().wait()  # run until killed
        finally:
            for w in workers:
                w.cancel()
            await runner.cleanup()


def run_daemon(instance: str, account: str, port: int, *,
               headed: bool, with_addon: bool) -> int:
    try:
        asyncio.run(_serve(instance, account, port, headed, with_addon))
    except KeyboardInterrupt:
        pass
    return 0
