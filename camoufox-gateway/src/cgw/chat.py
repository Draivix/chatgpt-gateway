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
from pathlib import Path

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
    # File attachments: the composer "+" button mounts a general (any-type) file input
    # (#upload-files, accept=""); after upload each file renders as a "file tile" chip.
    "attach_plus": '[data-testid="composer-plus-btn"]',
    "file_input": "#upload-files",
    "file_tile": 'form div[class*="file-tile"]',
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


# A ChatGPT conversation URL is https://chatgpt.com/c/<uuid>. Capture the id so a
# later ask can navigate straight back to it ("returning to conversations").
_CONV_ID_RE = re.compile(r"/c/([0-9a-fA-F-]{8,})")


def _normalize_chat_ref(ref: str) -> str | None:
    """Accept a full conversation URL or a bare id -> canonical https URL (or None)."""
    ref = (ref or "").strip()
    if not ref:
        return None
    m = _CONV_ID_RE.search(ref)
    cid = m.group(1) if m else (ref if re.fullmatch(r"[0-9a-fA-F-]{8,}", ref) else None)
    return f"{CHATGPT_NEW.rstrip('/')}/c/{cid}" if cid else None


async def _conversation_id(page) -> str:
    with contextlib.suppress(Exception):
        m = _CONV_ID_RE.search(page.url or "")
        if m:
            return m.group(1)
    return ""


async def _conversation_title(page) -> str:
    """Best-effort human title of the current conversation (browser tab title)."""
    with contextlib.suppress(Exception):
        t = (await page.title() or "").strip()
        # ChatGPT tab title is the chat name (falls back to "ChatGPT" when unnamed).
        if t and t.lower() != "chatgpt":
            return t
    return ""


async def open_conversation(page, ref: str) -> bool:
    """Navigate to an existing conversation by URL/id and wait for it to load.

    Returns True if a conversation composer became available. The chat may still
    be generating (opened while a prior turn streams) — callers that want the
    settled answer should follow with ``_wait_complete``.
    """
    url = _normalize_chat_ref(ref)
    if not url:
        return False
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector(SEL["composer"], timeout=20_000)
    except Exception:
        return False
    # Give the message list a moment to hydrate (assistant turns load async).
    with contextlib.suppress(Exception):
        await page.wait_for_selector(SEL["assistant"], timeout=8000)
    return True


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
    # Dismiss any lingering menu/overlay first: in headed mode a still-open effort
    # submenu from a prior attempt intercepts pointer events on the composer, and a
    # bare click() then hangs for the full default timeout (~45s). Escape-first +
    # a bounded click keeps a stuck attempt cheap instead of burning 45s each loop.
    await _escape(page, 1)
    with contextlib.suppress(Exception):
        await page.locator(SEL["composer"]).first.click(timeout=8000)
    await page.keyboard.press("Control+Shift+m")
    await page.wait_for_timeout(700)


# Effort indicator shown in the composer toolbar (the "Vysoká" / "Pro rozšířené"
# dropdown). Used to VERIFY a selection actually took (not just clicked a menu item).
_EFFORT_LABELS = ["Okamžitá", "Střední", "Vysoká", "Velmi vysoká",
                  "Pro rozšířené", "Pro Standardní"]


def _effort_target_label(effort: str) -> str | None:
    """Toolbar indicator label that means ``effort`` is already active, or None for
    an unknown/default effort (nothing specific to verify against)."""
    effort = (effort or "").lower()
    if effort in PRO_SUB:
        return PRO_SUB[effort]        # "Pro rozšířené" / "Pro Standardní"
    return EFFORT_MAP.get(effort)     # "Vysoká" etc., or None


def _effort_satisfied(current: str, effort: str) -> bool:
    """True if the composer's current effort indicator ``current`` already means
    ``effort`` is selected, so set_effort can skip the fragile menu dance entirely.

    Pure/decidable (no page) so it is unit-testable. The two Pro sub-modes both
    start with "Pro", so they are disambiguated on their distinctive word rather
    than a prefix match; plain levels require an exact label match to avoid the
    "Vysoká" vs "Velmi vysoká" prefix trap.
    """
    target = _effort_target_label(effort)
    if not target or not current:
        return False
    cur = current.strip().lower()
    tgt = target.lower()
    if tgt == "pro rozšířené":
        return "rozšíř" in cur
    if tgt == "pro standardní":
        return "standard" in cur and "rozšíř" not in cur
    return cur == tgt


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
        # ── Fast path: the effort persists across chats in this profile, so the
        # wanted level is usually ALREADY shown in the composer toolbar. Verify
        # first and skip the menu entirely — this is the common case and avoids the
        # fragile Ctrl+Shift+M dance (and its headed-mode click hang) altogether.
        cur0 = await _current_effort_label(page)
        if _effort_satisfied(cur0, effort):
            log(f"effort already {cur0!r}; skipping menu")
            if effort in PRO_SUB:
                return f"Pro · {PRO_SUB[effort]}"
            return EFFORT_MAP.get(effort, "default")

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


async def _send_disabled(page) -> bool | None:
    """True if the send button is disabled, False if enabled, None if absent."""
    return await page.evaluate(
        r"""() => { const b = document.querySelector(
              '[data-testid=send-button], #composer-submit-button');
            return b ? b.disabled : null; }""")


async def _composer_uploading(page) -> bool:
    """True while an upload spinner/progressbar is live inside the composer form."""
    try:
        return bool(await page.evaluate(
            r"""() => { const f = document.querySelector('#prompt-textarea')?.closest('form');
                  if (!f) return false;
                  return f.querySelectorAll('.animate-spin, [role=progressbar]').length > 0; }"""))
    except Exception:
        return False


async def _attach_files(page, files: list[str], progress=None) -> int:
    """Attach local files to the composer, one per '+' menu open, and wait for each
    upload to finish. Returns the number of file-tile chips present at the end.

    ChatGPT mounts a general (any-type) ``#upload-files`` input only while the
    composer "+" menu is open, so we reopen it per file. Upload completion is judged
    by the send button re-enabling (disabled -> enabled) with no spinner left.
    """
    paths = []
    for f in files:
        p = Path(f).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"attachment not found on gateway host: {p}")
        paths.append(str(p.resolve()))

    for i, path in enumerate(paths):
        if progress:
            progress(f"attaching {Path(path).name} ({i + 1}/{len(paths)})")
        # Open the "+" menu (mounts #upload-files). Tooltip overlays the button, so force.
        with contextlib.suppress(Exception):
            await page.locator(SEL["attach_plus"]).first.click(force=True)
            await page.wait_for_timeout(500)
        inp = page.locator(SEL["file_input"]).first
        if not await inp.count():
            # fallback: last file input on the page (menu variant / DOM change)
            inp = page.locator('input[type=file]').last
        await inp.set_input_files(path)
        # Wait: this file's tile present, spinner gone, send re-enabled.
        want = i + 1
        deadline = time.time() + 90
        while time.time() < deadline:
            await page.wait_for_timeout(600)
            if await _count(page, SEL["file_tile"]) >= want \
                    and not await _composer_uploading(page) \
                    and await _send_disabled(page) is False:
                break
    tiles = await _count(page, SEL["file_tile"])
    if progress:
        progress(f"attached {tiles} file(s)")
    return tiles


async def ask(page, prompt: str, *, effort: str = "pro",
              system: str | None = None, timeout_s: int = 1200,
              progress=None, cont: bool = False,
              files: list[str] | None = None,
              chat: str | None = None) -> dict:
    """Send a prompt and return {ok, text, effort, elapsed, conversation_*}. Serialized.

    ``cont=True`` continues the CURRENT conversation (does not start a new chat),
    so prior turns stay in context. Multi-turn dialogue: first call cont=False,
    follow-ups cont=True. Either way only the latest assistant message is returned.

    ``chat`` (URL or bare id) RESUMES a specific past conversation regardless of what
    the tab currently shows — this is "returning to conversations". With ``chat`` set
    and an empty ``prompt`` it is FETCH mode: reopen the chat and return its latest
    answer (waiting if it is still generating) without sending anything. Every result
    carries ``conversation_id`` / ``conversation_url`` / ``conversation_title`` so the
    caller can persist where to return.
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

    prompt = prompt or ""

    async def _conv_fields():
        cid = await _conversation_id(page)
        return {
            "conversation_id": cid,
            "conversation_url": f"{CHATGPT_NEW.rstrip('/')}/c/{cid}" if cid else "",
            "conversation_title": await _conversation_title(page),
        }

    try:
        if chat:
            # ── Resume a SPECIFIC past conversation (URL/id) ──
            _p("opening conversation")
            if not await open_conversation(page, chat):
                return {"ok": False, "error": f"conversation not found: {chat}",
                        "elapsed": round(time.time() - t0, 1)}
            label = "resumed"
            if not prompt.strip():
                # FETCH mode: just return the latest answer (wait if still generating).
                _p("fetching latest answer")
                prior = await _count(page, SEL["assistant_md"])
                if await _count(page, SEL["stop"]):
                    await _wait_complete(page, timeout_s, _p, max(prior - 1, 0))
                text = await _extract_latest(page)
                elapsed = round(time.time() - t0, 1)
                if not text:
                    return {"ok": False, "error": "no answer in conversation",
                            "elapsed": elapsed, **await _conv_fields()}
                _p(f"fetched in {elapsed}s")
                return {"ok": True, "text": text, "effort": label,
                        "elapsed": elapsed, **await _conv_fields()}
            # else: fall through and send the new prompt into this resumed chat.
        elif cont:
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

        if files:
            await _attach_files(page, files, progress=_p)

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
                    "elapsed": round(time.time() - t0, 1), **await _conv_fields()}

        elapsed = round(time.time() - t0, 1)
        _p(f"done in {elapsed}s")
        return {"ok": True, "text": text, "effort": label,
                "elapsed": elapsed, **await _conv_fields()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "elapsed": round(time.time() - t0, 1)}
