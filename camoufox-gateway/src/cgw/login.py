"""chatgpt.com web auto-login for a Camoufox page.

Walks OpenAI's web login (email -> password -> push-auth -> IMAP email-OTP -> any
interstitials), so the session cookies land in our persistent profile. Works headless;
fall back to ``cgw login --headed`` if OpenAI throws a CAPTCHA.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import email.utils
import imaplib
import re
import time
from email.message import Message

from .config import CHATGPT_URL, DEBUG_DIR

OTP_RE = re.compile(r"\b(\d{6})\b")
OPENAI_OTP_SENDERS = (
    "noreply@tm.openai.com",
    "otp@tm1.openai.com",
    "noreply@openai.com",
    "login@openai.com",
)


def log(msg: str) -> None:
    print(f"[login {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── IMAP OTP (blocking; call via asyncio.to_thread) ─────────────────────────

def _list_folders(M: imaplib.IMAP4_SSL) -> list[str]:
    typ, data = M.list()
    if typ != "OK":
        return ["INBOX"]
    out: list[str] = []
    line_re = re.compile(r'^\(.*?\)\s+(?:"[^"]*"|NIL)\s+(?:"([^"]+)"|(\S+))\s*$')
    for raw in data:
        line = raw.decode(errors="ignore") if isinstance(raw, bytes) else raw
        m = line_re.match(line)
        if not m:
            continue
        name = m.group(1) or m.group(2)
        if not name:
            continue
        low = name.lower()
        if low == "inbox" or "junk" in low or "spam" in low:
            out.append(name)
    if "INBOX" not in out:
        out.insert(0, "INBOX")
    return out


def _strip_html(s: str) -> str:
    """Drop <style>/<script> (full of spurious numbers), tags, then unescape."""
    import html as _html
    s = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_otp(msg: Message) -> str | None:
    candidates: list[str] = []
    subj = msg.get("Subject") or ""
    try:
        decoded = "".join(
            part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part
            for part, enc in email.header.decode_header(subj)
        )
    except Exception:
        decoded = subj
    candidates.append(decoded)
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", errors="ignore"
            )
        except Exception:
            continue
        candidates.append(_strip_html(body) if ct == "text/html" else body)
    # prefer a code adjacent to "code"/"kód" wording, else first plain 6-digit
    for text in candidates:
        m = re.search(r"(?:code|k[oó]d)\D{0,20}(\d{6})", text, re.I)
        if m:
            return m.group(1)
    for text in candidates:
        m = OTP_RE.search(text)
        if m:
            return m.group(1)
    return None


def imap_get_otp(imap_cfg: dict, mailbox: str, password: str,
                 since_epoch: float, timeout_s: int = 90) -> str:
    log(f"IMAP poll {mailbox} for OTP, timeout {timeout_s}s")
    host = imap_cfg["host"]
    port = imap_cfg.get("port", 993)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(host, port)
            M.login(mailbox, password)
            for folder in _list_folders(M):
                try:
                    M.select(folder, readonly=True)
                except Exception:
                    continue
                typ, data = M.search(None, "ALL")
                if typ != "OK":
                    continue
                ids = data[0].split()
                for mid in reversed(ids[-30:]):
                    _, md = M.fetch(mid, "(RFC822)")
                    if not md or not md[0]:
                        continue
                    msg = email.message_from_bytes(md[0][1])
                    sender = (msg.get("From") or "").lower()
                    if not any(s in sender for s in OPENAI_OTP_SENDERS):
                        continue
                    try:
                        ts = email.utils.parsedate_to_datetime(msg.get("Date")).timestamp()
                    except Exception:
                        ts = 0
                    if ts + 5 < since_epoch:
                        continue
                    code = _extract_otp(msg)
                    if code:
                        log(f"OTP {code} found in {folder} (from {sender})")
                        M.logout()
                        return code
            M.logout()
        except Exception as e:  # noqa: BLE001
            log(f"IMAP error: {e}; retry")
        time.sleep(3)
    raise TimeoutError(f"no OpenAI OTP within {timeout_s}s for {mailbox}")


# ── Page helpers (async) ────────────────────────────────────────────────────

COMPOSER_SEL = "#prompt-textarea, div[contenteditable='true'][id='prompt-textarea']"
EMAIL_SEL = "input[name=email], input#email-input, input[type=email], input[name=username]"
PWD_SEL = "input[name=password], input#password, input[type=password]"
OTP_SEL = "input[autocomplete=one-time-code], input[name=code], input[inputmode=numeric]"


async def _snap(page, debug: bool, label: str) -> None:
    if not debug:
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(DEBUG_DIR / f"login_{label}.png"), full_page=True)
        (DEBUG_DIR / f"login_{label}.html").write_text(await page.content())
        log(f"snap {label} url={page.url}")
    except Exception as e:  # noqa: BLE001
        log(f"snap {label} failed: {e}")


async def is_logged_in(page) -> bool:
    """True if a usable ChatGPT composer is on the current page."""
    try:
        loc = page.locator(COMPOSER_SEL).first
        return await loc.is_visible(timeout=2500)
    except Exception:
        return False


class RateLimited(Exception):
    """OpenAI returned 'too many attempts' — must back off a few minutes."""


async def _rate_limited(page) -> bool:
    try:
        body = (await page.inner_text("body"))[:3000].lower()
    except Exception:
        return False
    return ("max_check_attempts" in body or "too many attempts" in body
            or "příliš mnoho pokus" in body or "prilis mnoho pokus" in body)


async def _pass_cloudflare(page, debug: bool) -> None:
    """Wait out a Cloudflare 'Just a moment' interstitial if present."""
    for _ in range(20):
        title = (await page.title()) or ""
        if "just a moment" not in title.lower() and "moment" not in title.lower():
            return
        log("cloudflare interstitial; waiting")
        await asyncio.sleep(1.5)


async def _click_any(page, role: str, pattern: str, timeout: float = 1500) -> bool:
    try:
        btn = page.get_by_role(role, name=re.compile(pattern, re.I)).first
        if await btn.is_visible(timeout=timeout):
            await btn.click()
            return True
    except Exception:
        pass
    return False


async def _dismiss_cookies(page) -> None:
    """Accept/close the cookie consent banner — it re-hydrates and wipes form input."""
    for pat in (r"p[řr]ijmout\s+v[šs]echny", r"accept all", r"accept",
                r"odmítnout", r"reject", r"souhlas"):
        if await _click_any(page, "button", pat, timeout=800):
            await asyncio.sleep(0.4)
            return


async def _settle(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass


async def _fill_verify(page, selector: str, value: str) -> bool:
    """Fill a (possibly React-controlled) field and confirm the value stuck."""
    field = page.locator(selector).first
    await field.click()
    for _ in range(3):
        try:
            await field.fill("")
            await field.fill(value)
        except Exception:
            await field.type(value, delay=15)
        try:
            if (await field.input_value()) == value:
                return True
        except Exception:
            return True  # input_value unsupported -> assume ok
        await asyncio.sleep(0.3)
    return False


async def ensure_logged_in(page, acct: dict, *, debug: bool = False) -> bool:
    """Ensure the page's context has a logged-in chatgpt.com session.

    Returns True if logged in (already or after driving the flow).
    """
    await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
    await _pass_cloudflare(page, debug)
    # give the SPA a moment to render the composer before deciding we're logged out
    try:
        await page.wait_for_selector(COMPOSER_SEL, timeout=8000)
        log("already logged in")
        return True
    except Exception:
        pass
    if await is_logged_in(page):
        log("already logged in")
        return True

    log("not logged in; driving web login")
    started = time.time()
    await _snap(page, debug, "00_landing")
    await _dismiss_cookies(page)

    # chatgpt.com/auth/login shows the email modal directly. If it isn't present yet,
    # click a Log-in entry point first.
    if await page.locator(EMAIL_SEL).count() == 0:
        if not await _click_any(page, "button", r"log ?in|p[řr]ihl[aá]s|sign ?in"):
            await _click_any(page, "link", r"log ?in|p[řr]ihl[aá]s|sign ?in")
        await _pass_cloudflare(page, debug)
        try:
            await page.wait_for_selector(EMAIL_SEL, timeout=15_000)
        except Exception:
            await page.goto(CHATGPT_URL + "auth/login", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector(EMAIL_SEL, timeout=20_000)
    except Exception:
        await _snap(page, debug, "99_no_email_field")
        log(f"could not reach email field; url={page.url}")
        return await is_logged_in(page)

    await _settle(page)
    await _dismiss_cookies(page)

    # ── email ──
    log("filling email")
    if not await _fill_verify(page, EMAIL_SEL, acct["email"]):
        log("WARN email value would not stick")
    await _snap(page, debug, "01_email")
    await page.locator(EMAIL_SEL).first.press("Enter")

    # wait for password / OTP / logged-in
    try:
        await page.wait_for_function(
            """() => document.querySelector('input[type=password]')
                  || document.querySelector('input[autocomplete=one-time-code]')
                  || document.querySelector('input[name=code]')
                  || document.querySelector('#prompt-textarea')""",
            timeout=25_000,
        )
    except Exception as e:  # noqa: BLE001
        log(f"timeout after email: {e}")
    await _snap(page, debug, "02_after_email")

    # ── password ──
    await _dismiss_cookies(page)
    if await page.locator(PWD_SEL).count() > 0:
        log("filling password")
        await _fill_verify(page, PWD_SEL, acct["password"])
        await _snap(page, debug, "03_pwd")
        await page.locator(PWD_SEL).first.press("Enter")
        try:
            await page.wait_for_function(
                """() => document.querySelector('input[autocomplete=one-time-code]')
                      || document.querySelector('input[name=code]')
                      || document.querySelector('#prompt-textarea')
                      || location.href.includes('chatgpt.com')
                      || location.href.includes('push-auth')
                      || location.href.includes('mfa')
                      || location.href.includes('verification')""",
                timeout=25_000,
            )
        except Exception as e:  # noqa: BLE001
            log(f"timeout after password: {e}")
    await _snap(page, debug, "04_after_pwd")

    # ── push-auth ("approve on your device") -> fall back to email code ──
    if "push-auth" in page.url or await page.locator(
        "text=/Schválení přihlášení|Approve sign-?in|Approve login|Check your/i"
    ).count() > 0:
        log("push-auth screen; switching to email code")
        # button is "Zkusit pomocí e-mailu" / "Try another way" / "Use email"
        if not await _click_any(page, "button", r"e-?mail", timeout=3000):
            await _click_any(page, "button", r"another way|jin[ýy]m zp[ůu]sobem|try .*email|use email")
        started = time.time()
        try:
            await page.wait_for_selector(OTP_SEL, timeout=20_000)
        except Exception as e:  # noqa: BLE001
            log(f"no OTP field after push-auth fallback: {e}")
        await _snap(page, debug, "04b_after_pushauth")

    # ── OTP (email verification) ──
    if await page.locator(OTP_SEL).count() > 0:
        log("OTP screen detected")
        code = None
        for attempt in range(3):
            try:
                code = await asyncio.to_thread(
                    imap_get_otp, acct["imap"], acct["imap_user"],
                    acct["imap_password"], started, 70 if attempt == 0 else 60,
                )
                break
            except TimeoutError as e:
                log(f"OTP not yet ({e}); trying resend")
                if not await _click_any(page, "button", r"resend|znovu|poslat"):
                    await _click_any(page, "link", r"resend|znovu|poslat")
                started = time.time()
        if not code:
            await _snap(page, debug, "99_no_otp")
            return await is_logged_in(page)
        inputs = page.locator(OTP_SEL)
        n = await inputs.count()
        if n >= 6:
            for i, ch in enumerate(code):
                await inputs.nth(i).fill(ch)
        else:
            await _fill_verify(page, OTP_SEL, code)
        await _snap(page, debug, "05_otp")
        # single-field OTP needs the "Pokračovat"/Continue button; Enter doesn't submit
        if not await _click_any(page, "button", r"continue|pokra|verify|ov[ěe][řr]|submit"):
            try:
                await inputs.first.press("Enter")
            except Exception:
                pass
        try:
            await page.wait_for_function(
                """() => document.querySelector('#prompt-textarea')
                      || location.href.includes('chatgpt.com')
                      || /incorrect|nespr|invalid|chyb/i.test(document.body.innerText)""",
                timeout=20_000,
            )
        except Exception:
            pass

    # ── interstitials: "Stay signed in?", device verify, consent ──
    deadline = time.time() + 45
    while time.time() < deadline:
        if await is_logged_in(page):
            log("login complete")
            await _snap(page, debug, "06_done")
            return True
        if await _rate_limited(page):
            await _snap(page, debug, "98_ratelimited")
            raise RateLimited("OpenAI: too many attempts — wait a few minutes, then retry")
        clicked = await _click_any(
            page, "button",
            r"^(continue|pokra|stay|z[uů]stat|yes|ano|authorize|allow|povolit|accept|ok)\b",
        )
        if not clicked:
            await asyncio.sleep(1.5)

    ok = await is_logged_in(page)
    await _snap(page, debug, "07_final")
    log(f"login finished, logged_in={ok}, url={page.url}")
    return ok
