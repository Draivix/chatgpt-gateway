"""Drive a ChatGPT conversation on a logged-in Camoufox page.

The smoothness fix: completion is detected by the backend SSE stream
(``POST /backend-api/f/conversation``, content-type text/event-stream) closing —
authoritative and CSP-immune — with a DOM signal (stop-button gone +
copy-turn-action-button present) as a parallel fallback. No fragile text-stability
heuristic, generous timeout for extended thinking.

Validated against the live DOM (June 2026 chatgpt.com, Czech locale) via ``cgw probe``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time

from .login import is_logged_in

CHATGPT_NEW = "https://chatgpt.com/"

# Reasoning effort levels (Czech UI labels in the Ctrl+Shift+M menu), low -> high.
# Top level "Pro" has its own sub-intensity submenu (Pro Standardní / Pro rozšířené).
EFFORT_MAP = {
    "instant": "Okamžitá",
    "standard": "Střední",
    "medium": "Střední",
    "high": "Vysoká",
    "extended": "Velmi vysoká",
    "very-high": "Velmi vysoká",
}
# GPT-5.5 Pro sub-intensities. Default "pro" = the deepest (Pro rozšířené = Pro Extended).
PRO_SUB = {
    "pro": "Pro rozšířené",
    "pro-extended": "Pro rozšířené",
    "pro-max": "Pro rozšířené",
    "pro-standard": "Pro Standardní",
}
PRO_TRIGGER = '[data-testid="composer-intelligence-pro-thinking-effort-trigger"]'

SEL = {
    "composer": "#prompt-textarea",
    "send": '[data-testid="send-button"], #composer-submit-button',
    "stop": ('button[data-testid="stop-button"], button[aria-label*="Stop" i], '
             'button[aria-label*="Zastav" i]'),
    "assistant": '[data-message-author-role="assistant"]',
    "assistant_md": '[data-message-author-role="assistant"] .markdown',
    "done_marker": '[data-testid="copy-turn-action-button"]',
    "new_chat": ('[data-testid="create-new-chat-button"], a[href="/"]'),
}


def log(msg: str) -> None:
    print(f"[chat {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _is_conversation_sse(resp) -> bool:
    try:
        if resp.request.method != "POST":
            return False
        url = resp.url.split("?", 1)[0].rstrip("/")
        return url.endswith("/backend-api/f/conversation")
    except Exception:
        return False


async def new_chat(page) -> None:
    try:
        btn = page.locator(SEL["new_chat"]).first
        if await btn.count() and await btn.is_visible(timeout=2000):
            await btn.click()
            await page.wait_for_timeout(700)
            await page.wait_for_selector(SEL["composer"], timeout=15_000)
            return
    except Exception:
        pass
    await page.goto(CHATGPT_NEW, wait_until="domcontentloaded")
    await page.wait_for_selector(SEL["composer"], timeout=20_000)


async def _escape(page, n: int = 2) -> None:
    for _ in range(n):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(120)
        except Exception:
            return


async def _open_effort_menu(page) -> None:
    await page.locator(SEL["composer"]).first.click()
    await page.keyboard.press("Control+Shift+m")
    await page.wait_for_timeout(700)


# Effort indicator shown in the composer toolbar (the "Vysoká" / "Pro rozšířené"
# dropdown). Used to VERIFY a selection actually took (not just clicked a menu item).
_EFFORT_LABELS = ["Okamžitá", "Střední", "Vysoká", "Velmi vysoká",
                  "Pro rozšířené", "Pro Standardní"]


async def _current_effort_label(page) -> str:
    try:
        return (await page.evaluate(
            r"""(labels) => {
              for (const b of document.querySelectorAll('button, [role=button]')) {
                const t = (b.innerText || '').trim();
                if (labels.some(l => t === l || t.startsWith(l))) return t;
              }
              return '';
            }""", _EFFORT_LABELS)).strip()
    except Exception:
        return ""


async def set_effort(page, effort: str) -> str:
    """Open the reasoning-effort menu (Ctrl+Shift+M) and pick the level.

    Handles GPT-5.5 Pro's sub-intensity submenu (Pro Standardní / Pro rozšířené).
    Returns the chosen label (best-effort; never raises).
    """
    effort = (effort or "").lower()
    try:
        # ── Pro modes: main "Pro" group + sub-intensity submenu ──
        if effort in PRO_SUB:
            sub = PRO_SUB[effort]  # e.g. "Pro rozšířené"
            cur = ""
            for attempt in range(3):
                await _open_effort_menu(page)
                # The top-level Pro group's label reflects its current sub-mode
                # ("Pro Standardní" / "Pro rozšířené"), so a "^Pro$" match never hits
                # — match "Pro <word>" (or fall back to the submenu trigger element).
                pro = page.get_by_role("menuitemradio", name=re.compile(r"^Pro\s")).first
                if not await pro.count():
                    t0 = page.locator(PRO_TRIGGER).first
                    if not await t0.count():
                        await _escape(page)
                        continue
                    pro = t0
                with contextlib.suppress(Exception):
                    await pro.hover()
                    await page.wait_for_timeout(350)
                trig = page.locator(PRO_TRIGGER).first
                if await trig.count():
                    with contextlib.suppress(Exception):
                        await trig.click(force=True)
                        await page.wait_for_timeout(500)
                # Click the requested sub INSIDE the freshly opened submenu (last menu),
                # disambiguating it from the same-named top-level group item.
                submenu = page.locator('[role="menu"]').last
                subradio = submenu.get_by_role(
                    "menuitemradio", name=re.compile(re.escape(sub))).first
                if not await subradio.count():
                    subradio = page.get_by_role(
                        "menuitemradio", name=re.compile(re.escape(sub))).last
                if await subradio.count():
                    with contextlib.suppress(Exception):
                        await subradio.click()
                        await page.wait_for_timeout(400)
                await _escape(page)
                # VERIFY against the composer indicator — retry if it didn't take.
                cur = await _current_effort_label(page)
                if "rozšíř" in cur.lower() or cur.lower().startswith("pro"):
                    log(f"effort verified -> {cur!r} (wanted {sub!r})")
                    return f"Pro · {sub}"
                log(f"effort attempt {attempt+1} not yet Pro (indicator={cur!r}); retrying")
            log(f"effort UNVERIFIED, indicator stuck at {cur!r}")
            return f"pro-unverified:{cur or 'no-label'}"

        # ── plain levels ──
        label = EFFORT_MAP.get(effort)
        if not label:
            return "default"
        await _open_effort_menu(page)
        radio = page.get_by_role("menuitemradio", name=re.compile(re.escape(label))).first
        if not await radio.count():
            await _escape(page)
            return f"label-not-found:{label}"
        if (await radio.get_attribute("aria-checked")) != "true":
            await radio.click()
            await page.wait_for_timeout(400)
        await _escape(page)
        return label
    except Exception as e:  # noqa: BLE001
        log(f"set_effort failed: {e}")
        await _escape(page)
        return "error"


async def _composer_text(page) -> str:
    """Current text in the ProseMirror composer (empty string if blank/missing)."""
    try:
        return (await page.locator(SEL["composer"]).first.inner_text()).strip()
    except Exception:
        return ""


async def _enter_text(page, text: str) -> bool:
    """Put ``text`` into the contenteditable composer and VERIFY it landed.

    The composer is a ProseMirror contenteditable div; after the effort menu
    (Ctrl+Shift+M) focus can be lost, so a blind ``fill`` silently no-ops and the
    prompt is never pasted ("refreshing but not pasting"). We dismiss menus, retry
    a few ways, and confirm non-empty before returning.
    """
    composer = page.locator(SEL["composer"]).first
    for _ in range(4):
        await _escape(page, 2)               # close any lingering menu/overlay
        try:
            await composer.click()
        except Exception:
            pass
        try:
            await composer.fill("")
            await composer.fill(text)
        except Exception:
            try:
                await composer.click()
                await page.keyboard.insert_text(text)
            except Exception:
                with contextlib.suppress(Exception):
                    await composer.type(text[:6000], delay=1)
        await page.wait_for_timeout(200)
        if await _composer_text(page):
            return True
    # last resort: raw keyboard insert into the focused element
    with contextlib.suppress(Exception):
        await composer.click()
        await page.keyboard.insert_text(text)
        await page.wait_for_timeout(150)
    return bool(await _composer_text(page))


async def _send(page, text: str) -> None:
    if not await _enter_text(page, text):
        log("WARN composer stayed empty after retries; submitting anyway")
    await page.wait_for_timeout(150)
    # Submit, then confirm the composer cleared (= the message actually left the
    # box). Enter is primary; the send button (overlaid by its tooltip) is fallback.
    for _ in range(3):
        try:
            await page.locator(SEL["composer"]).first.press("Enter")
        except Exception:
            with contextlib.suppress(Exception):
                await page.locator(SEL["send"]).first.click(force=True)
        await page.wait_for_timeout(450)
        if not await _composer_text(page):
            return  # sent
    with contextlib.suppress(Exception):
        await page.locator(SEL["send"]).first.click(force=True)


async def _extract_latest(page) -> str:
    """Clean text of the latest assistant message (drops KaTeX's hidden MathML twin)."""
    try:
        return (await page.evaluate(r"""() => {
          const md = document.querySelectorAll('[data-message-author-role=assistant] .markdown');
          if (md.length) {
            const el = md[md.length - 1].cloneNode(true);
            el.querySelectorAll('.katex-mathml').forEach(n => n.remove());
            return (el.innerText || '').trim();
          }
          const a = document.querySelectorAll('[data-message-author-role=assistant]');
          return a.length ? (a[a.length - 1].innerText || '').trim() : '';
        }""")).strip()
    except Exception:
        md = page.locator(SEL["assistant_md"]).last
        if await md.count():
            return (await md.inner_text()).strip()
        return ""


async def _count(page, sel: str) -> int:
    try:
        return await page.locator(sel).count()
    except Exception:
        return 0


async def _wait_complete(page, timeout_s: int, progress, prior_count: int = 0) -> None:
    """Return when generation finishes, judged from the DOM.

    The ``/f/conversation`` SSE closes early (generation then streams over a
    websocket), so the authoritative signal is the Stop button: it is present for
    the whole turn (thinking + answering) and disappears when done. Confirmed by
    the answer's copy action button / stable text.

    ``prior_count`` = number of assistant messages already present before this
    turn was sent (non-zero on a continued conversation). Phase 1 must wait for a
    NEW assistant message (count > prior_count), not break on a pre-existing one.
    """
    deadline = time.time() + timeout_s
    # Phase 1: wait for the turn to START (Stop button appears), up to 120s.
    start_deadline = min(deadline, time.time() + 120)
    gen_started = False
    while time.time() < start_deadline:
        if await _count(page, SEL["stop"]):
            gen_started = True
            break
        # Only treat as "started/answered" when a NEW assistant message exists.
        if await _count(page, SEL["assistant_md"]) > prior_count and await _extract_latest(page):
            break
        await asyncio.sleep(0.5)
    if gen_started and progress:
        progress("thinking…")

    # Phase 2: wait for completion — Stop gone, answer text present + settled.
    last, stable = "", 0
    while time.time() < deadline:
        if await _count(page, SEL["stop"]):
            await asyncio.sleep(0.8)
            continue
        txt = await _extract_latest(page)
        if txt:
            stable = stable + 1 if txt == last else 0
            last = txt
            if await _count(page, SEL["done_marker"]) or stable >= 2:
                return
        await asyncio.sleep(0.7)


async def ask(page, prompt: str, *, effort: str = "pro",
              system: str | None = None, timeout_s: int = 1200,
              progress=None, cont: bool = False) -> dict:
    """Send a prompt and return {ok, text, model/effort, elapsed}. Serialized by daemon.

    ``cont=True`` continues the CURRENT conversation (does not start a new chat),
    so prior turns stay in context. Multi-turn dialogue: first call cont=False,
    follow-ups cont=True. Either way only the latest assistant message is returned.
    """
    t0 = time.time()
    if not await is_logged_in(page):
        return {"ok": False, "error": "not logged in"}

    def _p(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    try:
        if cont:
            # Continue the existing conversation: ensure a composer is present
            # (do NOT reset the thread). Effort persists from the prior turn.
            _p("continuing conversation")
            try:
                await page.wait_for_selector(SEL["composer"], timeout=15_000)
            except Exception:
                # No live conversation to continue -> fall back to a fresh chat.
                await new_chat(page)
                await set_effort(page, effort)
            label = "continue"
        else:
            _p("starting new chat")
            await new_chat(page)
            label = await set_effort(page, effort)
            _p(f"effort: {label}")

        prior_count = await _count(page, SEL["assistant_md"])
        msg = f"{system}\n\n{prompt}" if system else prompt
        await _send(page, msg)
        _p("sent; waiting for response (extended thinking can take minutes)")

        await _wait_complete(page, timeout_s, _p, prior_count)

        text = ""
        for _ in range(16):
            await page.wait_for_timeout(400)
            streaming = await _count(page, SEL["stop"])
            text = await _extract_latest(page)
            if text and not streaming:
                break
        if not text:
            text = await _extract_latest(page)
        if not text:
            return {"ok": False, "error": "no response captured",
                    "elapsed": round(time.time() - t0, 1)}

        elapsed = round(time.time() - t0, 1)
        _p(f"done in {elapsed}s")
        return {"ok": True, "text": text, "effort": label, "elapsed": elapsed}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "elapsed": round(time.time() - t0, 1)}
