# Camoufox ChatGPT-Pro gateway (`cgw`)

Talk to **ChatGPT Pro** (GPT-5.5, up to **Pro Extended** reasoning) programmatically —
**no API key** — by driving the real chatgpt.com web UI inside a headless **Camoufox**
(stealth Firefox) that stays logged in through a persistent browser profile. Exposed as a
**Claude Code skill + an MCP server** and a small **CLI**.

It is the optimized successor to the `chatgpt-gateway` browser extension in the parent
folder. The extension's core weakness was *knowing when a response is finished* — it
guessed from DOM streaming hints + a short timeout, so it truncated long answers and timed
out on reasoning models. Here a long-running daemon owns the browser and judges completion
from the page's real generation state, with a generous timeout, and hands work back via a
submit→poll job queue — so **extended/Pro answers come back complete and smooth**, and a
client never has to hold a multi-minute HTTP request.

---

## Table of contents

1. [Architecture](#architecture)
2. [Requirements](#requirements)
3. [Install (step by step)](#install-step-by-step)
4. [Credentials: `accounts.json`](#credentials-accountsjson)
5. [First login](#first-login)
6. [Run the daemon](#run-the-daemon)
7. [CLI reference](#cli-reference)
8. [Reasoning effort levels](#reasoning-effort-levels)
9. [Use from Claude Code (skill + MCP)](#use-from-claude-code-skill--mcp)
10. [Use from codex](#use-from-codex)
11. [Run as a systemd service](#run-as-a-systemd-service)
12. [How it works](#how-it-works)
13. [Troubleshooting](#troubleshooting)
14. [Security](#security)

---

## Architecture

```
Claude Code / codex ──stdio MCP──▶ cgw mcp ──HTTP 127.0.0.1:18791──▶ cgw serve (daemon, asyncio)
   CLI: cgw ask ─────────────────────────────────────────────────────▶   │
                                                                           │ owns:
                                                     AsyncCamoufox persistent_context
                                                       • profile/<account>/   (session cookies, gitignored)
                                                       • chatgpt-gateway extension loaded as a Firefox addon
                                                     ├ auto-login (email → password → push-auth → email-OTP via IMAP)
                                                     ├ select reasoning effort (instant … Pro Extended)
                                                     ├ completion judged from DOM generation state (not the SSE)
                                                     └ serialized job queue: POST /ask → job_id → GET /jobs/{id}
```

One browser, one tab → requests are **serialized** through a queue (fine for a personal
helper; not a high-throughput fleet).

---

## Requirements

- **Linux** (developed on Ubuntu; macOS likely works, untested).
- **[uv](https://docs.astral.sh/uv/)** (Python package/venv manager). Install:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- Python **3.11+** (uv will fetch one if needed).
- A **ChatGPT Pro** account, and **IMAP access to the mailbox** that receives OpenAI's
  email login codes (the gateway reads the 6-digit code over IMAP to log in unattended).
- Camoufox downloads a ~66 MB GeoIP DB and its Firefox build on first run (cached under
  `~/.cache/camoufox`). A graphical display is **not** required (headless).

---

## Install (step by step)

```bash
# 1. Get the repo
git clone https://github.com/Draivix/chatgpt-gateway.git ~/chatgpt-gateway
cd ~/chatgpt-gateway/camoufox-gateway

# 2. Create the venv and install dependencies (camoufox, aiohttp, mcp)
uv sync

# 3. Create your credentials file (see next section)
mkdir -p ~/.config/cgw
cp accounts.example.json ~/.config/cgw/accounts.json
chmod 600 ~/.config/cgw/accounts.json
$EDITOR ~/.config/cgw/accounts.json     # fill in real values

# 4. Log a browser profile into ChatGPT (headless auto-login; reads the email OTP)
uv run cgw login default

# 5. Start the daemon
uv run cgw serve default                 # foreground (Ctrl-C to stop)
#   …or install the systemd user service — see "Run as a systemd service"

# 6. Verify and ask
uv run cgw status
uv run cgw ask "Explain the CAP theorem in 3 bullets."     # defaults to Pro Extended
```

`<account>` in the commands below is a key from your `accounts.json` (e.g. `default`).
If you omit it, the value of `$CGW_ACCOUNT` is used (default: `default`).

---

## Credentials: `accounts.json`

The repo contains **no credentials**. They live in a JSON file outside the repo —
default `~/.config/cgw/accounts.json`, or anywhere you point `CGW_ACCOUNTS_FILE`.

Schema (see `accounts.example.json`):

```json
{
  "imap": { "host": "mail.example.com", "port": 993, "ssl": true },
  "accounts": {
    "default": {
      "email": "you@example.com",
      "password": "YOUR_CHATGPT_PASSWORD",
      "imap_user": "you@example.com",
      "imap_password": "YOUR_MAILBOX_PASSWORD"
    }
  }
}
```

- `email` / `password` — your ChatGPT (OpenAI) login.
- `imap_*` — the mailbox that receives the OpenAI login code. Defaults to `email` /
  `password` if omitted. OpenAI usually delivers the code to **Spam/Junk**; the poller
  scans INBOX + Junk + Spam.
- Add multiple accounts under `accounts` and select with `CGW_ACCOUNT=<key>` or a
  positional arg. `chmod 600` the file.

> Use a dedicated/disposable ChatGPT account you control. Automating the web UI is your
> responsibility re: OpenAI's terms.

---

## First login

```bash
uv run cgw login <account>            # headless auto-login
uv run cgw login <account> --headed   # show the window (needed only if a CAPTCHA appears)
uv run cgw login <account> --debug    # dump step screenshots/HTML into debug/
```

What happens: Camoufox opens chatgpt.com, fills email → password, handles the
**"approve sign-in"** screen by switching to the **email code**, reads the 6-digit code
over IMAP, submits it, and lands logged in. The session is saved in
`profile/<account>/` and reused on every subsequent run.

If OpenAI shows a CAPTCHA (rare, usually only from a brand-new IP), run once with
`--headed`, solve it in the window, and the session then persists for headless use.

---

## Run the daemon

```bash
uv run cgw serve <account>            # headless (default)
uv run cgw serve <account> --headed   # visible browser window
uv run cgw serve <account> --no-addon # don't load the extension addon
```

The daemon owns the browser for its whole lifetime, auto-logs-in on startup if needed,
and listens on `http://127.0.0.1:18791` (override with `CGW_HOST` / `CGW_PORT`).

---

## CLI reference

| Command | What it does |
|---|---|
| `cgw login [account] [--headed] [--debug] [--no-addon] [--hold N]` | Log a persistent profile into chatgpt.com. `--hold N` = seconds to keep a headed window open for a manual finish. |
| `cgw serve [account] [--headed] [--no-addon]` | Run the gateway daemon. |
| `cgw ask "<message>" [--effort LEVEL] [--timeout SECONDS]` | One-shot ask via the running daemon. Prints the answer to stdout, progress to stderr. |
| `cgw status` | Daemon health (JSON: account, logged_in, busy, queued). |
| `cgw accounts` | List account keys from your `accounts.json`. |
| `cgw mcp` | Run the stdio MCP server (used by Claude Code / codex; not for humans). |
| `cgw probe [account]` | Dev tool: re-capture the live ChatGPT DOM/network if OpenAI changes the UI. Writes to `debug/`. |

Environment variables: `CGW_ACCOUNT`, `CGW_ACCOUNTS_FILE`, `CGW_HOST`, `CGW_PORT`,
`CGW_ASK_TIMEOUT`.

Examples:

```bash
uv run cgw ask "Refactor this regex and explain why: ^(?=.*\d).{8,}$"
uv run cgw ask "Quick: capital of Japan?" --effort instant
uv run cgw ask "Prove the halting problem is undecidable." --effort pro --timeout 900
CGW_ACCOUNT=second uv run cgw serve     # run the daemon on a different account
```

---

## Reasoning effort levels

ChatGPT exposes a reasoning-effort menu (the composer pill / `Ctrl+Shift+M`). `--effort`
maps to it, low → high:

| `--effort` | ChatGPT level | Notes |
|---|---|---|
| `instant` | Okamžitá (Instant) | fastest, shallow |
| `standard` | Střední (Medium) | |
| `high` | Vysoká (High) | |
| `extended` | Velmi vysoká (Very High) | deep, non-Pro |
| `pro-standard` | **Pro** → Pro Standardní | GPT-5.5 Pro, standard depth |
| `pro` *(default)* / `pro-extended` | **Pro** → Pro rozšířené | **GPT-5.5 Pro Extended — deepest reasoning** |

"Pro" has its own sub-intensity submenu; the gateway opens it and selects the level.
Pro/Pro-Extended answers can take **minutes** — that's expected; the default timeout is
1200 s (20 min). The UI labels are localized (Czech here); the selectors live in
`src/cgw/chat.py` (`EFFORT_MAP` / `PRO_SUB`).

---

## Use from Claude Code (skill + MCP)

**Skill** (optional convenience): a `chatgpt-pro` skill that tells Claude how/when to use
the tools. Copy it into your skills dir, adjusting paths/accounts to your setup:

```bash
mkdir -p ~/.claude/skills/chatgpt-pro
$EDITOR ~/.claude/skills/chatgpt-pro/SKILL.md
```

**MCP server** — register it in your Claude Code MCP config (commonly
`~/.claude/mcp_servers.json`, or a project `.mcp.json`):

```json
{
  "mcpServers": {
    "chatgpt-pro": {
      "command": "uv",
      "args": ["run", "--project", "/ABSOLUTE/PATH/TO/chatgpt-gateway/camoufox-gateway", "cgw", "mcp"]
    }
  }
}
```

Restart Claude Code. Tools exposed:

- `chatgpt_status` — is the gateway up and logged in? (call first)
- `chatgpt_ask(message, effort="pro", timeout=1200, system=None)` — ask and get the
  answer; polls to completion and reports progress.
- `chatgpt_poll(job_id)` — check a long job already submitted.
- `chatgpt_login()` — trigger a headless re-login.

The MCP server is a thin client — the **daemon must be running** (`cgw serve` or the
systemd unit). `chatgpt_status` tells you if it isn't.

---

## Use from codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.chatgpt]
command = "uv"
args = ["run", "--project", "/ABSOLUTE/PATH/TO/chatgpt-gateway/camoufox-gateway", "cgw", "mcp"]
startup_timeout_sec = 30.0
```

---

## Run as a systemd service

Keeps the daemon up across logins/reboots, with the browser warm.

```bash
# optional: put your real account + creds path here (gitignored, machine-local)
mkdir -p ~/.config/cgw
cat > ~/.config/cgw/cgw.env <<'EOF'
CGW_ACCOUNT=default
CGW_ACCOUNTS_FILE=%h/.config/cgw/accounts.json
EOF

cp systemd/cgw-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cgw-gateway
systemctl --user status cgw-gateway
journalctl --user -u cgw-gateway -f         # logs

# optional: keep it running even when you're not logged in
loginctl enable-linger "$USER"
```

The unit uses `%h` (your home) and an optional `EnvironmentFile`, so it works unmodified
if you cloned to `~/chatgpt-gateway` and installed uv to `~/.local/bin`. Adjust
`WorkingDirectory`/`ExecStart` otherwise.

---

## How it works

- **Login** (`login.py`): reuses the email → password → **push-auth → email-code** flow
  and reads the OTP over IMAP. Robustness baked in: dismiss the cookie banner (it
  re-hydrates and wipes the email field); strip `<style>`/tags from HTML-only OTP mails
  (they contain decoy 6-digit numbers); click the "Continue" button for single-field OTP
  (Enter doesn't submit); detect and back off on "too many attempts".
- **Completion** (`chat.py`): the `/backend-api/f/conversation` SSE **closes early** (the
  real stream continues over a websocket), so completion is judged from the DOM —
  the **Stop button** is present for the whole turn (thinking + answering) and disappears
  when done, confirmed by the answer's copy button / stable text. Generous timeout. The
  answer is read from the rendered `.markdown` (KaTeX's hidden MathML twin is stripped to
  avoid duplicated math).
- **Daemon** (`daemon.py`): asyncio job queue + aiohttp loopback API; auto-clears a stale
  Firefox profile lock (only when the owning PID is dead).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `chatgpt_status`/`cgw status` says daemon not reachable | Start it: `cgw serve` or `systemctl --user start cgw-gateway`. |
| `logged_in: false` | `cgw login <account>`; if a CAPTCHA shows, `cgw login <account> --headed`. |
| "too many attempts" / `max_check_attempts` | OpenAI rate-limited login retries — wait a few minutes, then retry. Don't loop logins. |
| Rate-limit window exhausted | Each Pro account has 5h + 7d windows; switch account (`CGW_ACCOUNT=...`). |
| Browser won't start: profile "in use" | A stale lock; the daemon auto-clears dead-PID locks. If a real instance is running, stop it first. |
| Headless fails needing a display | Prefix `ExecStart`/command with `xvfb-run -a`. |
| Answers look wrong / empty after a ChatGPT redesign | Re-run `cgw probe <account>`, then update the selectors in `src/cgw/chat.py` (`SEL`, `EFFORT_MAP`, `PRO_SUB`). |
| Effort label not found | The UI is localized; check the labels in `chat.py` match your account's language. |

---

## Security

- `profile/` holds **live ChatGPT session cookies** — gitignored; never commit it.
- `debug/` holds login screenshots/HTML (may contain your email / an OTP) — gitignored.
- `accounts.json` lives **outside** the repo (`~/.config/cgw/…`), `chmod 600`.
- The loopback API binds `127.0.0.1` only.
