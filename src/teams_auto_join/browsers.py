"""Find out which browser the user wants: the OS default one, or an explicitly chosen one.

Supported: Chrome, Edge, Brave (all Chromium based) and Firefox.
Unknown defaults (Safari, Opera, ...) fall back to the first supported browser that is installed.
"""
from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("teams_auto_join")

SUPPORTED = ("chrome", "msedge", "brave", "firefox", "chromium")
ALIASES = {
    "edge": "msedge", "microsoft-edge": "msedge", "microsoftedge": "msedge",
    "google-chrome": "chrome", "googlechrome": "chrome",
    "brave-browser": "brave",
    "ff": "firefox",
    "default": "auto", "system": "auto",
}
# order used when the default browser is not supported / cannot be detected
FALLBACK_ORDER = ("chrome", "brave", "msedge", "firefox", "chromium")


def normalize_browser(name: str | None) -> str:
    value = (name or "auto").strip().lower()
    value = ALIASES.get(value, value)
    if value != "auto" and value not in SUPPORTED:
        raise ValueError(f"Unknown browser {name!r}. Use one of: auto, {', '.join(SUPPORTED)}.")
    return value


def _from_identifier(ident: str) -> str | None:
    """Map a ProgId / bundle id / .desktop file name to one of our browser names."""
    s = ident.lower()
    if "brave" in s:
        return "brave"
    if "edge" in s or "msedge" in s:
        return "msedge"
    if "firefox" in s or "mozilla" in s:
        return "firefox"
    if "chromium" in s:
        return "chromium"
    if "chrome" in s:  # after edge/chromium on purpose: "ChromeHTML", "google-chrome.desktop", "com.google.chrome"
        return "chrome"
    return None


# --------------------------------------------------------------------------- default browser per OS
def _default_windows() -> str | None:
    import winreg  # noqa: PLC0415 - Windows only

    key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
        prog_id, _ = winreg.QueryValueEx(handle, "ProgId")
    return _from_identifier(prog_id)


def _default_macos() -> str | None:
    path = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    with path.open("rb") as fh:
        handlers = plistlib.load(fh).get("LSHandlers", [])
    for entry in handlers:
        if entry.get("LSHandlerURLScheme") == "https":
            return _from_identifier(entry.get("LSHandlerRoleAll") or entry.get("LSHandlerRoleViewer") or "")
    return None


def _default_linux() -> str | None:
    for cmd in (["xdg-settings", "get", "default-web-browser"], ["xdg-mime", "query", "default", "x-scheme-handler/https"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        found = _from_identifier(out) if out else None
        if found:
            return found
    return None


def detect_default_browser() -> str | None:
    """Return 'chrome' | 'msedge' | 'brave' | 'firefox' | 'chromium', or None if unknown/unsupported."""
    try:
        if sys.platform.startswith("win"):
            return _default_windows()
        if sys.platform == "darwin":
            return _default_macos()
        return _default_linux()
    except Exception as exc:  # noqa: BLE001 - detection is best effort
        log.debug("Could not detect the default browser: %s", exc)
        return None


# --------------------------------------------------------------------------- executables
def brave_executable() -> str | None:
    """Playwright has no 'brave' channel, so we point it at the executable."""
    candidates: list[str] = []
    if sys.platform.startswith("win"):
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates.append(str(Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe"))
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            str(Path.home() / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        ]
    else:
        for name in ("brave-browser", "brave-browser-stable", "brave"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        candidates += ["/opt/brave.com/brave/brave-browser", "/opt/brave.com/brave/brave", "/usr/bin/brave-browser"]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return None


def is_installed(browser: str) -> bool | None:
    """True/False when we can tell cheaply, None when Playwright has to find out by trying (chrome, msedge, firefox)."""
    if browser == "brave":
        return brave_executable() is not None
    return None


def candidate_order(wanted: str) -> list[str]:
    """Browsers to try, best first. 'auto' = the OS default browser, then the fallbacks."""
    if wanted != "auto":
        return [wanted]
    default = detect_default_browser()
    if default:
        log.info("System default browser: %s", default)
    else:
        log.info("Could not detect (or does not support) the system default browser - using the first installed supported one.")
    order = [default] if default else []
    order += [b for b in FALLBACK_ORDER if b not in order]
    return order
