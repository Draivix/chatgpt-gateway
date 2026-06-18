"""Camoufox persistent-context launch (with the chatgpt-gateway plugin loaded)."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .config import EXTENSION_DIR, SUBPROJECT_DIR, profile_dir


def clear_stale_lock(account: str) -> None:
    """Remove a Firefox profile lock left by a crashed/killed browser.

    Only removes it when the owning PID is dead, so a live daemon is never disturbed.
    """
    d = profile_dir(account)
    lock = d / "lock"
    try:
        target = os.readlink(lock)
    except OSError:
        return
    m = re.search(r"(\d+)\s*$", target)
    pid = int(m.group(1)) if m else None
    if pid and Path(f"/proc/{pid}").exists():
        return  # still held by a live process
    for name in ("lock", ".parentlock"):
        try:
            (d / name).unlink()
        except OSError:
            pass

ADDON_BUILD = SUBPROJECT_DIR / "addon-build"
_ADDON_FILES = ("background.js", "content.js", "popup.html", "popup.js")


def build_addon() -> Path | None:
    """Assemble a Firefox-loadable copy of the chatgpt-gateway extension.

    The repo ships a Chrome ``manifest.json`` and a ``manifest.firefox.json``; an
    unpacked Firefox addon needs the Firefox manifest *as* ``manifest.json``.
    Returns the addon dir, or None if the source files are missing.
    """
    src_manifest = EXTENSION_DIR / "manifest.firefox.json"
    if not src_manifest.exists():
        return None
    try:
        ADDON_BUILD.mkdir(parents=True, exist_ok=True)
        (ADDON_BUILD / "manifest.json").write_text(src_manifest.read_text())
        for name in _ADDON_FILES:
            srcf = EXTENSION_DIR / name
            if srcf.exists():
                shutil.copy2(srcf, ADDON_BUILD / name)
        icons_src = EXTENSION_DIR / "icons"
        if icons_src.is_dir():
            shutil.copytree(icons_src, ADDON_BUILD / "icons", dirs_exist_ok=True)
        # sanity: manifest parses
        json.loads((ADDON_BUILD / "manifest.json").read_text())
        return ADDON_BUILD
    except Exception:  # noqa: BLE001 — addon is best-effort; never block launch
        return None


def camoufox_kwargs(account: str, *, headless: bool, with_addon: bool = True) -> dict:
    """Build kwargs for ``AsyncCamoufox(**kwargs)`` for a persistent profile."""
    kwargs: dict = {
        "persistent_context": True,
        "user_data_dir": str(profile_dir(account)),
        "headless": headless,
        "humanize": True,
        "locale": ["cs-CZ", "en-US"],
        "geoip": True,
        "os": ("linux",),
        # ChatGPT keeps a tab open for a long time; let it use a real window size.
        "window": (1280, 900),
    }
    if with_addon:
        addon = build_addon()
        if addon:
            kwargs["addons"] = [str(addon)]
    return kwargs


async def first_page(context):
    """Return the context's existing page, or open one."""
    pages = context.pages
    return pages[0] if pages else await context.new_page()
