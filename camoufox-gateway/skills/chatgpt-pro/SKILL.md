---
name: chatgpt-pro
description: Talk to ChatGPT Pro (GPT-5.5, extended thinking) from an AI coding agent (Claude Code, codex, …) via a persistent Camoufox browser session — no API key, uses a logged-in ChatGPT Pro account. Use when the user wants a second opinion from ChatGPT/GPT-5.5, "ask ChatGPT Pro", "ask GPT-5", "extended thinking", "deep think this", cross-check an answer with ChatGPT, resume a past ChatGPT conversation, or run a hard prompt through ChatGPT Pro's reasoning. Triggers: "ask chatgpt", "chatgpt pro", "gpt-5.5", "extended thinking".
---

# ChatGPT Pro gateway (Camoufox)

Drive ChatGPT Pro's **web** UI through a Camoufox (stealth Firefox) that stays logged
into a ChatGPT Pro account via a persistent profile. Exposed as MCP tools and a `cgw`
CLI. The hard part — knowing when an extended-thinking answer is *done* — is solved by
watching the page's real generation state, so calls return the complete answer.

This skill ships **inside the gateway repo** (`camoufox-gateway/skills/chatgpt-pro/`).
Install it for your agent by copying it into your skills dir, e.g.:

```bash
cp -r <repo>/camoufox-gateway/skills/chatgpt-pro ~/.claude/skills/
```

The daemon must be running (`cgw serve`); the MCP server / CLI are thin clients over it.
See `camoufox-gateway/README.md` for full install, credentials, and MCP registration.

## MCP tools

1. `chatgpt_status` — verify the gateway is up and logged in. **Call this first.**
2. `chatgpt_ask` — ask and get the answer (markdown). Key params:
   - `message` — the prompt (optional only in fetch mode, see `chat`).
   - `effort` (low→high): `instant`, `standard`, `high`, `extended` (Very High),
     `pro-standard` (GPT-5.5 Pro), **`pro` = `pro-extended` (GPT-5.5 Pro Extended,
     deepest reasoning, DEFAULT)**. Pro modes can take minutes.
   - `timeout` seconds (default 1200), `system` (optional preamble).
   - `instance` — which named session (run parallel Pro conversations on separate
     profiles/daemons); defaults to the configured default instance.
   - `cont` — continue that instance's current conversation instead of starting fresh.
   - `chat` — **RESUME a specific past conversation** by URL or id. With `chat` set and
     an empty `message`, it just fetches that chat's latest answer (fetch mode).
   - `files` — local file paths (on the gateway host) to attach for ChatGPT to read.
   - Every answer ends with a `⟨conversation: URL⟩` footer — persist it to return later.
3. `chatgpt_poll job_id` — check a long job already submitted.
4. `chatgpt_login` — trigger a headless re-login if the session lapsed.
5. `chatgpt_instances` — list named sessions and whether each daemon is up.
6. `chatgpt_conversations` — list recorded conversations (`last_used | turns | title |
   url`) so a *new* session can find a past chat to resume.

Default to `effort=pro` (GPT-5.5 Pro Extended). Use `instant`/`standard` only when the
user explicitly wants a quick, shallow reply.

## CLI (equivalent, for humans / scripts)

```bash
cgw status                                   # daemon health (call first)
cgw ask "your prompt"                         # defaults to Pro Extended
cgw ask "quick q" --effort instant
cgw ask "follow-up" --continue                # keep the tab's current thread
cgw chats                                     # list resumable conversations
cgw ask --chat <url|id> "follow-up prompt"    # RESUME a specific past chat, full context
cgw ask --chat <url|id>                       # fetch mode: just read its latest answer
```

## Returning to a conversation (persistent memory)

Conversations are **not** one-shot. Every ask records the chat it touched to
`~/.config/cgw/conversations.json` (id → url, title, turns, last_used, instance), so a
*fresh* agent instance can look up and re-enter a thread it never saw created.

- `--continue` only tracks the tab's *current* chat and resets on daemon restart;
  `--chat <url|id>` + the on-disk store are the **durable** resume path (survive restarts).
- `cgw chats` reads the store even if the daemon is down.
- ChatGPT keeps every chat server-side, so any conversation URL is always re-openable in
  a headed window (`cgw serve <instance> --headed`) even if it predates the store.

## If the gateway is down

```bash
cd <repo>/camoufox-gateway
uv run cgw serve            # foreground; or:
systemctl --user start cgw-gateway     # if the unit is installed
uv run cgw status           # health
```

## Notes / gotchas

- One browser, one tab per instance → requests are **serialized** (queued). Run multiple
  named instances for parallelism.
- The ChatGPT UI may be localized (effort labels differ by account language); selectors
  live in `src/cgw/chat.py` (`SEL`, `EFFORT_MAP`, `PRO_SUB`).
- Each Pro account has 5h + 7d rate-limit windows; if exhausted, switch account/instance.
- Never commit `profile/` (live session cookies) — it is gitignored.
