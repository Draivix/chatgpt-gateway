"""Dev probe: capture the live ChatGPT DOM (composer, model picker, extended-thinking
control) and the backend SSE shape, so actuation can be written against reality.

Run after a profile is logged in:  uv run cgw probe <account>
Outputs land in debug/probe_*.
"""

from __future__ import annotations

import json
import re

from . import config
from .browser import camoufox_kwargs, first_page
from .config import CHATGPT_URL, DEBUG_DIR
from .login import ensure_logged_in, is_logged_in


async def run_probe(instance: str, account: str, headed: bool) -> int:
    from camoufox.async_api import AsyncCamoufox

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    acct = config.load_account(account)
    kw = camoufox_kwargs(instance, headless=not headed)
    async with AsyncCamoufox(**kw) as ctx:
        page = await first_page(ctx)

        # capture all backend-api traffic; remember conversation SSE bodies
        net: list[dict] = []
        sse_bodies: dict[str, str] = {}

        async def on_response(resp):
            try:
                url = resp.url
                if "/backend-api/" not in url and "conversation" not in url:
                    return
                ct = resp.headers.get("content-type", "")
                rec = {"url": url, "status": resp.status, "ct": ct,
                       "method": resp.request.method}
                net.append(rec)
                if "conversation" in url and resp.request.method == "POST":
                    try:
                        body = await resp.text()
                        sse_bodies[url] = body[:20000]
                    except Exception as e:  # noqa: BLE001
                        sse_bodies[url] = f"<read failed: {e}>"
            except Exception:
                pass

        page.on("response", on_response)

        if not await ensure_logged_in(page, acct, debug=True):
            print("[probe] NOT logged in — run `cgw login` first")
            return 2
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        await page.screenshot(path=str(DEBUG_DIR / "probe_00_home.png"), full_page=False)

        report: dict = {"url": page.url}

        # composer
        for sel in ("#prompt-textarea", "div[contenteditable=true]", "textarea"):
            report[f"composer::{sel}"] = await page.locator(sel).count()

        # model picker — dump button + open menu
        picker_sels = [
            '[data-testid="model-switcher-dropdown-button"]',
            '[data-testid="model-switcher"]',
            'button[aria-haspopup="menu"]',
            'button[aria-label*="model" i]',
        ]
        picker_found = None
        for sel in picker_sels:
            if await page.locator(sel).count() > 0:
                picker_found = sel
                break
        report["model_picker_sel"] = picker_found
        if picker_found:
            try:
                await page.locator(picker_found).first.click()
                await page.wait_for_timeout(1200)
                await page.screenshot(path=str(DEBUG_DIR / "probe_01_modelmenu.png"))
                items = page.locator('[role="menuitem"], [role="option"], [role="menuitemradio"]')
                n = await items.count()
                menu = []
                for i in range(min(n, 40)):
                    try:
                        menu.append((await items.nth(i).inner_text()).strip()[:80])
                    except Exception:
                        pass
                report["model_menu_items"] = menu
                (DEBUG_DIR / "probe_modelmenu.html").write_text(await page.content())
                # look for a submenu / effort control mentioning thinking/extended
                report["mentions_thinking"] = await page.locator(
                    "text=/thinking|extended|rozš|přemýšl|reasoning/i"
                ).count()
                await page.keyboard.press("Escape")
            except Exception as e:  # noqa: BLE001
                report["model_menu_error"] = str(e)

        # send a tiny prompt to learn the SSE endpoint + completion shape
        try:
            composer = page.locator("#prompt-textarea").first
            await composer.click()
            await composer.fill("Reply with exactly the single word: PONG")
            await page.wait_for_timeout(300)
            send = page.locator(
                '[data-testid="send-button"], #composer-submit-button, '
                'button[aria-label*="Send" i], button[data-testid="composer-send-button"]'
            ).first
            await send.click()
            # wait for an assistant turn to finish (regenerate/copy buttons appear)
            await page.wait_for_timeout(1500)
            done = False
            for _ in range(40):
                await page.wait_for_timeout(1500)
                streaming = await page.locator(
                    'button[data-testid="stop-button"], button[aria-label*="Stop" i]'
                ).count()
                assistant = await page.locator('[data-message-author-role="assistant"]').count()
                if assistant and not streaming:
                    done = True
                    break
            report["completion_done"] = done
            last = page.locator('[data-message-author-role="assistant"]').last
            report["assistant_text"] = (await last.inner_text())[:300] if await last.count() else ""
            # markdown container probe
            report["assistant_markdown_sel"] = await page.locator(
                '[data-message-author-role="assistant"] .markdown'
            ).count()
        except Exception as e:  # noqa: BLE001
            report["send_error"] = str(e)

        report["network"] = net[-40:]
        report["sse_endpoints"] = list(sse_bodies.keys())
        for i, (url, body) in enumerate(sse_bodies.items()):
            (DEBUG_DIR / f"probe_sse_{i}.txt").write_text(f"URL: {url}\n\n{body}")
        # show distinct SSE event types
        evt_types = set()
        for body in sse_bodies.values():
            evt_types |= set(re.findall(r'"type"\s*:\s*"([^"]+)"', body))
        report["sse_event_types"] = sorted(evt_types)[:40]

        (DEBUG_DIR / "probe_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(json.dumps({k: v for k, v in report.items() if k != "network"},
                         indent=2, ensure_ascii=False)[:4000])
        return 0
