"""Paths, accounts, and runtime config for the Camoufox ChatGPT-Pro gateway."""

from __future__ import annotations

import json
import os
from pathlib import Path

# camoufox-gateway/src/cgw/config.py -> repo = chatgpt-gateway/
PKG_DIR = Path(__file__).resolve().parent
SUBPROJECT_DIR = PKG_DIR.parents[1]          # .../chatgpt-gateway/camoufox-gateway
REPO_DIR = SUBPROJECT_DIR.parent             # .../chatgpt-gateway  ("this folder")

# Persistent browser profiles live UNDER this folder (one per account). They hold
# live ChatGPT session cookies -> gitignored, mode 700.
PROFILE_BASE = SUBPROJECT_DIR / "profile"
DEBUG_DIR = SUBPROJECT_DIR / "debug"

# The chatgpt-gateway browser extension ("the plugin") to load into Camoufox.
EXTENSION_DIR = REPO_DIR

# Credentials live in a JSON file OUTSIDE this repo (mode 600). Default location is
# ~/.config/cgw/accounts.json; override with the CGW_ACCOUNTS_FILE env var. See
# accounts.example.json for the schema. This repo never contains real credentials.
ACCOUNTS_FILE = Path(
    os.environ.get(
        "CGW_ACCOUNTS_FILE",
        str(Path.home() / ".config" / "cgw" / "accounts.json"),
    )
)

# Which account key (from accounts.json) to use by default. Override with CGW_ACCOUNT.
DEFAULT_ACCOUNT = os.environ.get("CGW_ACCOUNT", "default")

# Loopback HTTP API the MCP server talks to.
DAEMON_HOST = os.environ.get("CGW_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("CGW_PORT", "18791"))
DAEMON_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}"

CHATGPT_URL = "https://chatgpt.com/"

# Extended-thinking responses can run many minutes; be generous.
ASK_TIMEOUT_S = int(os.environ.get("CGW_ASK_TIMEOUT", "1200"))  # 20 min hard cap


def profile_dir(account: str) -> Path:
    p = PROFILE_BASE / account
    p.mkdir(parents=True, exist_ok=True)
    try:
        PROFILE_BASE.chmod(0o700)
        p.chmod(0o700)
    except OSError:
        pass
    return p


def load_account(account: str) -> dict:
    """Return {email, password, imap_user, imap_password, imap: {...}} for an account."""
    with ACCOUNTS_FILE.open() as f:
        data = json.load(f)
    accts = data.get("accounts", {})
    if account not in accts:
        raise SystemExit(
            f"unknown account '{account}'. available: {sorted(accts)} "
            f"(from {ACCOUNTS_FILE})"
        )
    a = accts[account]
    return {
        "email": a["email"],
        "password": a["password"],
        "imap_user": a.get("imap_user", a["email"]),
        "imap_password": a.get("imap_password", a["password"]),
        "imap": data["imap"],
    }


def list_accounts() -> list[str]:
    try:
        with ACCOUNTS_FILE.open() as f:
            return sorted(json.load(f).get("accounts", {}))
    except OSError:
        return []
