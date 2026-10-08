"""List the profiles of the user's installed browser, let them pick one, and prepare a working copy of it.

Why a copy? Since Chrome 136 (and Edge/Brave, which share the engine) refuse to be automated on their *default* user-data
folder, the chosen profile (cookies, saved sign-ins, extensions' storage...) is copied into
<project>/browser-profiles/<browser>/ (configurable) and the automation runs on that copy. Your real profile is only ever READ.
The copy is made once and reused (so its session keeps itself fresh); use --refresh-profile to copy again.
"""
from __future__ import annotations

import configparser
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

log = logging.getLogger("teams_auto_join")

HOME = Path.home()


def default_store() -> Path:
    """<project folder>/browser-profiles. Falls back to the current folder if the package is not run from the project."""
    project = Path(__file__).resolve().parents[2]
    base = project if (project / "pyproject.toml").is_file() else Path.cwd()
    return base / "browser-profiles"


@dataclass(frozen=True)
class BrowserProfile:
    dir_name: str  # chromium: "Default" / "Profile 1" ; firefox: the folder name
    label: str  # what the user recognises: "Sara (sara@company.com)"
    path: Path  # the real profile folder
    root: Path  # chromium: the "User Data" folder ; firefox: folder containing the profiles


@dataclass(frozen=True)
class Prepared:
    user_data_dir: Path
    profile_arg: str | None  # chromium: value for --profile-directory
    description: str


# --------------------------------------------------------------------------- where profiles live
def _chromium_roots(browser: str) -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData/Local"))
    support = HOME / "Library/Application Support"
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
    table = {
        "chrome": [local / "Google/Chrome/User Data", support / "Google/Chrome", cfg / "google-chrome"],
        "msedge": [local / "Microsoft/Edge/User Data", support / "Microsoft Edge", cfg / "microsoft-edge"],
        "brave": [local / "BraveSoftware/Brave-Browser/User Data", support / "BraveSoftware/Brave-Browser",
                  cfg / "BraveSoftware/Brave-Browser"],
        "chromium": [cfg / "chromium", support / "Chromium", local / "Chromium/User Data"],
    }
    return table.get(browser, [])


def _firefox_roots() -> list[Path]:
    appdata = Path(os.environ.get("APPDATA", HOME / "AppData/Roaming"))
    return [
        appdata / "Mozilla/Firefox",
        HOME / "Library/Application Support/Firefox",
        HOME / ".mozilla/firefox",
        HOME / ".config/mozilla/firefox",
        HOME / "snap/firefox/common/.mozilla/firefox",
        HOME / ".var/app/org.mozilla.firefox/.mozilla/firefox",
    ]


def _list_chromium(browser: str) -> list[BrowserProfile]:
    for root in _chromium_roots(browser):
        state = root / "Local State"
        if not state.is_file():
            continue
        try:
            cache = json.loads(state.read_text(encoding="utf-8")).get("profile", {}).get("info_cache", {})
        except (OSError, ValueError):
            cache = {}
        found = []
        for dir_name, info in cache.items():
            path = root / dir_name
            if not path.is_dir():
                continue
            name = info.get("name") or dir_name
            who = info.get("user_name") or info.get("gaia_name") or ""
            found.append(BrowserProfile(dir_name, f"{name} ({who})" if who else name, path, root))
        if not found and (root / "Default").is_dir():
            found.append(BrowserProfile("Default", "Default", root / "Default", root))
        if found:
            return sorted(found, key=lambda p: (p.dir_name != "Default", p.dir_name.lower()))
    return []


def _list_firefox() -> list[BrowserProfile]:
    for root in _firefox_roots():
        ini = root / "profiles.ini"
        if not ini.is_file():
            continue
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(ini, encoding="utf-8")
        except configparser.Error:
            continue
        defaults = {cp[s].get("Default") for s in cp.sections() if s.startswith("Install")}
        found = []
        for sec in cp.sections():
            if not sec.startswith("Profile"):
                continue
            raw = cp[sec].get("Path")
            if not raw:
                continue
            path = root / raw if cp[sec].get("IsRelative", "1") == "1" else Path(raw)
            if not path.is_dir():
                continue
            name = cp[sec].get("Name", path.name)
            star = " [default]" if raw in defaults or cp[sec].get("Default") == "1" else ""
            found.append(BrowserProfile(path.name, f"{name}{star}", path, root))
        if found:
            return found
    return []


def list_profiles(browser: str) -> list[BrowserProfile]:
    return _list_firefox() if browser == "firefox" else _list_chromium(browser)


# --------------------------------------------------------------------------- remembered choice
def _load_choices(store: Path) -> dict[str, str]:
    try:
        return json.loads((store / "choice.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember(store: Path, browser: str, dir_name: str) -> None:
    data = _load_choices(store)
    data[browser] = dir_name
    try:
        store.mkdir(parents=True, exist_ok=True)
        (store / "choice.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- choosing
def _match(profiles: list[BrowserProfile], wanted: str) -> BrowserProfile | None:
    w = wanted.strip().lower()
    if w.isdigit() and 1 <= int(w) <= len(profiles):
        return profiles[int(w) - 1]
    for p in profiles:
        if w in (p.dir_name.lower(), p.label.lower()) or w == p.label.split(" (")[0].lower():
            return p
    return None


def _ask_which(browser: str, profiles: list[BrowserProfile], default: BrowserProfile) -> BrowserProfile:
    print(f"\nWhich {browser} profile should be used for Teams?")
    for i, p in enumerate(profiles, 1):
        print(f"  {i}) {p.label}   [{p.dir_name}]")
    default_idx = profiles.index(default) + 1
    while True:
        try:
            answer = input(f"Enter a number [{default_idx}]: ").strip()
        except EOFError:
            return default
        if not answer:
            return default
        chosen = _match(profiles, answer)
        if chosen:
            return chosen
        print(f"  Please type a number between 1 and {len(profiles)}.")


_cache: dict[str, BrowserProfile | None] = {}  # one question per browser per run (preflight + join share the answer)


def choose_profile(store: Path, browser: str, wanted: str | None, always_ask: bool) -> BrowserProfile | None:
    if browser in _cache:
        return _cache[browser]
    profiles = list_profiles(browser)
    chosen: BrowserProfile | None = None
    if not profiles:
        log.info("No existing %s profiles found - a fresh dedicated profile will be used.", browser)
    elif wanted and wanted.lower() != "ask":
        chosen = _match(profiles, wanted)
        if not chosen:
            options = ", ".join(f"{p.dir_name} = {p.label}" for p in profiles)
            raise ConfigError(f"No {browser} profile matches {wanted!r}. Available: {options}")
    else:
        remembered = _load_choices(store).get(browser)
        current = next((p for p in profiles if p.dir_name == remembered), None)
        interactive = bool(sys.stdin and sys.stdin.isatty())
        must_ask = (wanted or "").lower() == "ask" or always_ask or current is None
        if len(profiles) == 1:
            chosen = profiles[0]
        elif interactive and must_ask:
            chosen = _ask_which(browser, profiles, current or profiles[0])
        else:
            chosen = current or profiles[0]
            log.info("Using %s profile %r (pick another with --profile ask or --profile <name>).", browser, chosen.label)
    if chosen:
        _remember(store, browser, chosen.dir_name)
    _cache[browser] = chosen
    return chosen


# --------------------------------------------------------------------------- cloning
_SKIP_NAMES = {
    # caches / junk that are big and useless for a sign-in
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "Crashpad", "BrowserMetrics", "Media Cache", "CacheStorage", "ScriptCache", "component_crx_cache",
    "optimization_guide_model_store", "Safe Browsing", "extensions_crx_cache",
    # your open tabs / last session: restoring them is what opened extra pages in the automation window
    "Sessions", "Current Session", "Current Tabs", "Last Session", "Last Tabs",
    # locks left by a running browser
    "SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile", "lock", ".parentlock", "parent.lock",
    # firefox caches
    "cache2", "startupCache", "crashes", "minidumps", "saved-telemetry-pings", "datareporting",
}


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in _SKIP_NAMES}


def _safe_copy(src: str, dst: str) -> str:
    try:
        shutil.copy2(src, dst)
    except OSError:  # file locked by the running browser: skip it, the rest is usually enough
        log.debug("Skipped locked file: %s", src)
    return dst


def _copy_tree(src: Path, dst: Path) -> None:
    try:
        shutil.copytree(src, dst, ignore=_ignore, copy_function=_safe_copy, dirs_exist_ok=True)
    except shutil.Error as exc:
        log.debug("Some files could not be copied: %s", exc)


_SESSION_FILES = ("Sessions", "Current Session", "Current Tabs", "Last Session", "Last Tabs")


def _forget_open_tabs(profile_folder: Path) -> None:
    """Copies made by an older version may contain the saved tab session: remove it so no old tabs reopen."""
    for name in _SESSION_FILES:
        target = profile_folder / name
        try:
            shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
        except OSError:
            pass


def prepare_profile(store: Path, browser: str, wanted: str | None, refresh: bool, always_ask: bool) -> Prepared | None:
    """Returns None when the browser has no existing profile (caller then uses a fresh dedicated one)."""
    profile = choose_profile(store, browser, wanted, always_ask)
    if profile is None:
        return None
    clone_root = store / browser
    if browser == "firefox":
        target = clone_root / profile.dir_name
        user_data, arg = target, None
        if refresh and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        fresh = not target.exists()
        if fresh:
            log.info("Copying your Firefox profile %r (one time)...", profile.label)
            _copy_tree(profile.path, target)
    else:
        target = clone_root / profile.dir_name
        user_data, arg = clone_root, profile.dir_name
        if refresh and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        fresh = not target.exists()
        if fresh:
            log.info("Copying your %s profile %r (one time; close that browser first for the freshest session)...",
                     browser, profile.label)
            clone_root.mkdir(parents=True, exist_ok=True)
            _safe_copy(str(profile.root / "Local State"), str(clone_root / "Local State"))  # holds the cookie-decryption key
            _copy_tree(profile.path, target)
        _forget_open_tabs(target)
    return Prepared(user_data, arg, f"{profile.label} (copy in {user_data})")
