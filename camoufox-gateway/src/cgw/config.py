"""Paths, accounts, and runtime config for the Camoufox ChatGPT-Pro gateway."""

from __future__ import annotations

import fcntl
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

# An INSTANCE is one named browser session = one Camoufox profile + one daemon + one
# port. The instance name (not the account) keys the profile dir, so several named
# sessions can run on the same account (each its own login). Default instance name
# follows CGW_ACCOUNT so the legacy single-daemon setup keeps working unchanged.
DEFAULT_INSTANCE = os.environ.get("CGW_INSTANCE", DEFAULT_ACCOUNT)

# Where the instance registry (name -> {port, account}) lives.
STATE_DIR = Path(os.environ.get("CGW_STATE_DIR", str(Path.home() / ".config" / "cgw")))
INSTANCES_FILE = Path(
    os.environ.get("CGW_INSTANCES_FILE", str(STATE_DIR / "instances.json")))


def _envbool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Run the browser with a visible window? Default headless (background daemon). Set
# CGW_HEADED=true (e.g. in ~/.config/cgw/cgw.env) to watch it; --headed/--headless
# on the CLI override this per-invocation.
HEADED = _envbool("CGW_HEADED", False)

# Loopback HTTP API. Each named INSTANCE (session) runs its own daemon bound to its
# own port: the first instance uses this base port, the rest get base+1, base+2, …
# allocated in the instance registry (see below). CGW_PORT pins a specific port
# (single-instance / back-compat); when set it overrides registry allocation.
DAEMON_HOST = os.environ.get("CGW_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("CGW_PORT", "18791"))     # base port (= default instance)
_PORT_FORCED = "CGW_PORT" in os.environ
DAEMON_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}"          # back-compat (default instance)


def daemon_url(port: int) -> str:
    return f"http://{DAEMON_HOST}:{port}"

CHATGPT_URL = "https://chatgpt.com/"

# Extended-thinking responses can run many minutes; be generous.
ASK_TIMEOUT_S = int(os.environ.get("CGW_ASK_TIMEOUT", "1200"))  # 20 min hard cap

# Number of browser windows the daemon drives in parallel. NOTE: Playwright-Firefox
# opens each page as a separate WINDOW (not a tab), and N concurrent ChatGPT windows
# on one account is heavy + trips slow cold-loads, so the default is 1: the job queue
# already serializes concurrent agents so they don't collide, and the reliability +
# watchdog fixes make each job dependable. Raise CGW_WORKERS only if you accept N
# windows + the rate-limit exposure.
WORKERS = max(1, int(os.environ.get("CGW_WORKERS", "1")))

# Navigation/action timeouts. chatgpt.com is a heavy SPA that under a headed cold
# start can take well over the Playwright 30s default to reach domcontentloaded.
NAV_TIMEOUT_MS = int(os.environ.get("CGW_NAV_TIMEOUT_MS", "60000"))
ACTION_TIMEOUT_MS = int(os.environ.get("CGW_ACTION_TIMEOUT_MS", "45000"))


def profile_dir(instance: str) -> Path:
    p = PROFILE_BASE / instance
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


def resolve_account(instance: str, explicit: str | None = None) -> str:
    """Account creds an instance uses: explicit flag > a same-named account > default.

    So ``cgw serve work`` (no matching account) runs the *default* account under a
    separate ``work`` profile, while ``cgw serve second`` still uses the ``second``
    account if one exists — preserving the pre-instance behaviour.
    """
    if explicit:
        return explicit
    if instance in set(list_accounts()):
        return instance
    return DEFAULT_ACCOUNT


# ── Instance registry (name -> {port, account}) ──────────────────────────────

def load_instances() -> dict:
    try:
        with INSTANCES_FILE.open() as f:
            return json.load(f).get("instances", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _save_instances(instances: dict) -> None:
    INSTANCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INSTANCES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"instances": instances}, indent=2))
    tmp.replace(INSTANCES_FILE)


def allocate_port(instance: str, account: str, explicit: int | None = None) -> int:
    """Reserve (and persist) the port for an instance, under a cross-process lock.

    Reuses the instance's recorded port if it has one; otherwise grabs the lowest
    free port from the base. ``explicit`` (``--port`` / ``CGW_PORT``) always wins.
    """
    INSTANCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = INSTANCES_FILE.with_suffix(".lock")
    with lock.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            instances = load_instances()
            rec = instances.get(instance, {})
            if explicit is not None:
                port = explicit
            elif rec.get("port"):
                port = int(rec["port"])
            else:
                used = {int(r["port"]) for r in instances.values() if r.get("port")}
                port = DAEMON_PORT
                while port in used:
                    port += 1
            instances[instance] = {"port": port, "account": account}
            _save_instances(instances)
            return port
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def instance_port(instance: str) -> int | None:
    """Resolve an instance's port for a CLIENT (read-only; no allocation).

    Falls back to the base port for the default instance so ``cgw ask`` works
    before anything has been registered (legacy single-daemon layout).
    """
    rec = load_instances().get(instance)
    if rec and rec.get("port"):
        return int(rec["port"])
    if instance == DEFAULT_INSTANCE:
        return DAEMON_PORT
    return None
