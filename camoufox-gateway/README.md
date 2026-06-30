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
7. [Multiple instances (named sessions)](#multiple-instances-named-sessions--parallel-pro-conversations)
8. [CLI reference](#cli-reference)
9. [Reasoning effort levels](#reasoning-effort-levels)
10. [Use from Claude Code (skill + MCP)](#use-from-claude-code-skill--mcp)
11. [Use from codex](#use-from-codex)
12. [Run as a systemd service](#run-as-a-systemd-service)
13. [How it works](#how-it-works)
14. [Troubleshooting](#troubleshooting)
15. [Security](#security)

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
uv run cgw serve <account>            # headless or headed per CGW_HEADED (default headless)
uv run cgw serve <account> --headed   # force a visible browser window
uv run cgw serve <account> --headless # force no window (override CGW_HEADED=true)
uv run cgw serve <account> --no-addon # don't load the extension addon
```

Headed vs headless is **config-driven** via `CGW_HEADED` (`true`/`1`/`yes`/`on` →
headed; default `false` → headless). Set it in `~/.config/cgw/cgw.env` (read by the
systemd unit) or the environment. `--headed` / `--headless` override it per call. Applies
to `serve`, `login`, and `probe`.

The daemon owns the browser for its whole lifetime, auto-logs-in on startup if needed,
and listens on `http://127.0.0.1:18791` (override with `CGW_HOST` / `CGW_PORT`).

---

## Multiple instances (named sessions) — parallel Pro conversations

An **instance** is one named browser session: its own Camoufox profile (`profile/<name>`),
its own login, its own daemon, its own port. Run several at once to hold **independent
Pro conversations in parallel** instead of queueing behind one minutes-long answer.

```bash
uv run cgw login work               # log the "work" profile in (own browser/login)
uv run cgw login research
uv run cgw serve work               # daemon for "work"  -> auto-port 18791
uv run cgw serve research           # daemon for "research" -> auto-port 18792
uv run cgw instances                # list sessions + live health
uv run cgw ask "..." --instance work
uv run cgw ask "..." --instance research --continue   # keep that session's thread
```

- **Instance name vs account.** The name keys the *profile*; creds come from the
  `--account` flag, else a same-named `accounts.json` key, else the default account. So
  `cgw serve work` runs the **default** account under a separate `work` profile — two
  named sessions on one ChatGPT account, each its own login. Point instances at different
  `accounts.json` keys (`--account second`) for full account isolation.
- **Ports** are allocated once per instance (base `18791`, then `+1`, …) and recorded in
  the registry at `~/.config/cgw/instances.json`; reused on restart. `--port N` / `CGW_PORT`
  pin one explicitly.
- **Same account, N sessions** = N concurrent logins on one ChatGPT plan → watch for
  OpenAI rate-limiting. Separate accounts per instance avoids it.
- **Back-compat:** with no instance name everything targets the default instance on the
  base port exactly as before.

systemd (one supervised daemon per instance) via the template unit — see *Run as a
service* below.

---

## CLI reference

| Command | What it does |
|---|---|
| `cgw login [instance] [--account KEY] [--headed\|--headless] [--debug] [--no-addon] [--hold N]` | Log a persistent profile (named session) into chatgpt.com. `--hold N` = seconds to keep a headed window open for a manual finish. |
| `cgw serve [instance] [--account KEY] [--port N] [--headed\|--headless] [--no-addon]` | Run the gateway daemon for one named instance. |
| `cgw ask "<message>" [--instance NAME] [--effort LEVEL] [--timeout SECONDS] [--continue]` | One-shot ask via the running daemon. Prints the answer to stdout, progress to stderr. `--continue` keeps the instance's current conversation. |
| `cgw status [--instance NAME]` | Daemon health (JSON: instance, account, logged_in, busy, queued). |
| `cgw instances` | List named instances (sessions) from the registry + each daemon's live state. |
| `cgw accounts` | List account keys from your `accounts.json`. |
| `cgw mcp` | Run the stdio MCP server (used by Claude Code / codex; not for humans). |
| `cgw probe [instance] [--account KEY]` | Dev tool: re-capture the live ChatGPT DOM/network if OpenAI changes the UI. Writes to `debug/`. |

The `[instance]` positional defaults to `CGW_INSTANCE` (itself defaulting to `CGW_ACCOUNT`),
so omitting it everywhere reproduces the original single-daemon behaviour.

Environment variables: `CGW_ACCOUNT`, `CGW_ACCOUNTS_FILE`, `CGW_INSTANCE`, `CGW_STATE_DIR`,
`CGW_INSTANCES_FILE`, `CGW_HEADED`, `CGW_HOST`, `CGW_PORT`, `CGW_ASK_TIMEOUT`.

Examples:

```bash
uv run cgw ask "Refactor this regex and explain why: ^(?=.*\d).{8,}$"
uv run cgw ask "Quick: capital of Japan?" --effort instant
uv run cgw ask "Prove the halting problem is undecidable." --effort pro --timeout 900
uv run cgw ask "Continue that proof." --instance research --continue
CGW_ACCOUNT=second uv run cgw serve     # default instance on a different account
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

### One service per instance (template unit)

For multiple named sessions, use the template unit `cgw-gateway@.service` — the part after
`@` is the instance name (`%i`). Log each profile in once, then enable a service per
instance:

```bash
cp systemd/cgw-gateway@.service ~/.config/systemd/user/
systemctl --user daemon-reload

uv run cgw login work && uv run cgw login research   # once per profile
systemctl --user enable --now cgw-gateway@work
systemctl --user enable --now cgw-gateway@research
uv run cgw instances                                 # verify both are up

# per-instance overrides (e.g. a different account) go in cgw-<instance>.env:
echo 'CGW_ACCOUNT=second' > ~/.config/cgw/cgw-research.env
```

Each instance gets its own profile and an auto-allocated port (registry:
`~/.config/cgw/instances.json`). The singleton `cgw-gateway.service` and the template can
coexist — the singleton serves the default instance.

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
