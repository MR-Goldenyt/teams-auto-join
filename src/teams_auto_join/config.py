"""Settings. Every input can come from the CLI, the environment / .env file, or a terminal prompt.

Precedence: CLI flag > real environment variable > .env file > interactive prompt > default.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .browsers import normalize_browser
from .profiles import default_store
from .errors import ConfigError

log = logging.getLogger("teams_auto_join")

TEAMS_HOSTS = ("teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft", "gov.teams.microsoft.us")
URL_IN_TEXT = re.compile(r"(?:https?://|msteams:)[^\s<>\"']+", re.I)
WINDOW_RE = re.compile(
    r"^\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?)\s*(?:-|–|—|\bto\b)\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?)\s*$",
    re.I,
)


@dataclass(frozen=True)
class Settings:
    url: str | None  # None only in --login mode
    name: str  # only used if the profile is NOT signed in and REQUIRE_LOGIN=false
    join_at: str | None  # None => see window; "now" => immediately
    window: str | None
    tz: tzinfo
    screenshot_dir: Path
    lobby_timeout_min: float
    stay_min: float
    leave_after_screenshot: bool
    max_attempts: int
    grace_min: float
    roll_over: bool
    browser: str  # "auto" = the system default browser, or chrome | msedge | brave | firefox | chromium
    profile: str | None  # which EXISTING browser profile to use (name / folder / number / "ask"); None = remembered or ask
    refresh_profile: bool  # copy the chosen profile again (picks up a newer sign-in)
    profile_store: Path  # where the copied browser profiles (and the remembered choice) are kept
    profile_dir: Path | None  # advanced: use this exact user-data folder as-is (no profile picking, no copy)
    screenshot_interval_min: float  # 0 = only the screenshot at join time
    require_login: bool
    preflight: bool
    no_sandbox: bool  # only for Linux containers / root where the browser sandbox cannot start
    login_only: bool
    dry_run: bool


# --------------------------------------------------------------------------- URL
def normalize_url(raw: str) -> str:
    """Accept a bare link, a quoted link, a whole pasted invite, a Safe Links wrapper or msteams: link."""
    text = (raw or "").strip().strip("\"'<>")
    if not text:
        raise ConfigError("The meeting link is empty.")
    found = URL_IN_TEXT.findall(text)
    if not found:
        if re.search(r"teams\.(microsoft|live)\.com", text, re.I):
            found = ["https://" + text.lstrip("/")]
        else:
            raise ConfigError(f"No URL found in: {text[:80]!r}")
    url = next((u for u in found if "teams" in u.lower() or "safelinks" in u.lower()), found[0])
    url = url.rstrip(".,;>]\"'")
    if url.lower().startswith("msteams:"):
        url = "https://teams.microsoft.com/" + url[len("msteams:"):].lstrip("/")

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("safelinks.protection.outlook.com"):
        inner = parse_qs(parsed.query).get("url")
        if not inner:
            raise ConfigError("Outlook Safe Links wrapper without an inner URL.")
        return normalize_url(unquote(inner[0]))
    if not any(host == h or host.endswith("." + h) for h in TEAMS_HOSTS):
        raise ConfigError(f"'{host or url}' is not a Microsoft Teams link.")
    where = (parsed.path + "#" + parsed.fragment).lower()  # web-client links keep the route in the #fragment
    if not any(seg in where for seg in ("/meet/", "/meetup-join", "/l/meeting", "/dl/launcher")):
        raise ConfigError("This looks like a Teams link but not a *meeting* link (expected .../meet/... or .../meetup-join/...).")
    return url


def web_join_urls(url: str) -> list[str]:
    """Candidate URLs that open the meeting in the Teams *web app*, skipping 'launcher.html'.

    launcher.html is what triggers Chrome's native "Open Microsoft Teams?" popup, which automation can't click.
    The last candidate is always the original link as a fallback.
    """
    p = urlparse(url)
    host = (p.hostname or "teams.microsoft.com").lower()
    if "/dl/launcher" in p.path:
        inner = parse_qs(p.query).get("url", [""])[0]  # e.g. "/_#/meet/123?p=abc&anon=true"
        route = inner.split("#", 1)[1] if "#" in inner else inner
    elif p.fragment.startswith("/"):  # already a web-client link: .../_#/meet/123?p=abc
        route = p.fragment
    else:
        route = p.path + (f"?{p.query}" if p.query else "")
    rpath, _, rquery = route.partition("?")
    if not (rpath.startswith("/meet/") or rpath.startswith("/l/meetup-join")):
        return [url]
    pairs = [(k, v) for k, v in parse_qsl(rquery, keep_blank_values=True) if k.lower() != "anon"]  # anon=true = "join as guest"
    web = f"https://{host}/v2/?meetingjoin=true#{rpath}" + (f"?{urlencode(pairs)}" if pairs else "")
    return [web] if web == url else [web, url]


# --------------------------------------------------------------------------- helpers
def _pick(cli, env_key: str, default=None):
    if cli is not None:
        return cli
    val = os.getenv(env_key)
    if val is not None and val.strip().strip("\"'") != "":
        return val.strip().strip("\"'")
    return default


def _as_bool(val, name: str, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    raise ConfigError(f"{name} must be true/false, got {val!r}.")


def _as_num(val, name: str, default: float, cast=float, minimum: float | None = None):
    if val is None:
        return default
    try:
        num = cast(val)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number, got {val!r}.") from None
    if minimum is not None and num < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {num}.")
    return num


def _ask(prompt: str, default: str | None = None) -> str | None:
    if not sys.stdin or not sys.stdin.isatty():
        return None
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return None
    return answer or default


def _resolve_tz(name: str | None) -> tzinfo:
    if not name:
        return datetime.now().astimezone().tzinfo  # system local zone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"Unknown timezone {name!r}. Use an IANA name like 'Asia/Bahrain' or 'Europe/London'.") from None


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    B = argparse.BooleanOptionalAction
    p = argparse.ArgumentParser(
        prog="teams-join",
        description="Join a Teams meeting as a guest - muted, camera off - and screenshot the moment you're in.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  teams-join 'https://teams.microsoft.com/meet/123?p=abc' --window 09:55-10:05\n"
            "  teams-join --url '<link>' --join-at 14:30 --name 'Sara'\n"
            "  teams-join --now --no-headless          # link etc. read from .env\n"
            "  teams-join                              # asks for whatever is missing\n"
        ),
    )
    p.add_argument("url", nargs="?", help="Teams meeting link (or whole invite text)")
    p.add_argument("--url", dest="url_opt", help="same as the positional link")
    p.add_argument("--join-at", help="exact time: 14:30 | 2:30pm | '2026-10-09 14:30' | +10m | now")
    p.add_argument("--window", help="random time inside a window, e.g. 09:55-10:05")
    p.add_argument("--now", action="store_true", help="join immediately")
    p.add_argument("--name", help="guest display name (only used if the profile is not signed in and --no-require-login)")
    p.add_argument("--timezone", help="IANA timezone for the times above (default: system)")
    p.add_argument("--screenshot-dir", help="base folder for screenshots (default: screenshots)")
    p.add_argument("--lobby-timeout", type=float, help="minutes to wait in the lobby (default 30)")
    p.add_argument("--stay-minutes", type=float, help="minutes to stay after joining (default 120 = 2 hours); 0 = until meeting ends")
    p.add_argument("--screenshot-interval", type=float, help="minutes between screenshots while in the meeting (default 20; 0 = only at join)")
    p.add_argument("--leave-after-screenshot", action=B, default=None, help="leave right after the screenshot")
    p.add_argument("--max-attempts", type=int, help="join attempts before giving up (default 3)")
    p.add_argument("--grace-minutes", type=float, help="join immediately if the time passed less than this ago (default 15)")
    p.add_argument("--roll-over", action=B, default=None, help="if the time passed long ago, schedule for tomorrow")
    p.add_argument("--browser", "--browser-channel", dest="browser",
                   help="auto (= your system default browser, the default) | chrome | msedge | brave | firefox | chromium")
    p.add_argument("--profile", help="which of your existing browser profiles to use: its name, folder (e.g. 'Profile 1'), a number, or 'ask'")
    p.add_argument("--refresh-profile", action="store_true", help="copy the chosen profile again (use after you signed in to Teams in your normal browser)")
    p.add_argument("--profile-store", help="folder where copied browser profiles are kept (default: <project>/browser-profiles)")
    p.add_argument("--profile-dir", help="advanced: use this exact user-data folder as-is instead of copying one of your profiles")
    p.add_argument("--login", action="store_true", help="open the browser once so you can sign in to Teams, then exit")
    p.add_argument("--require-login", action=B, default=None, help="refuse to join as a guest (default on)")
    p.add_argument("--preflight", action=B, default=None, help="check browser + sign-in before waiting (default on)")
    p.add_argument("--no-sandbox", action="store_true", default=None, help="disable the browser sandbox (only for Linux containers/root; shows a warning banner)")
    p.add_argument("--env-file", help="path to .env (default: ./.env)")
    p.add_argument("--dry-run", action="store_true", help="only show the planned join time, don't open a browser")
    return p


def load_settings(argv: list[str] | None = None) -> Settings:
    args = build_parser().parse_args(argv)

    env_path = Path(args.env_file) if args.env_file else Path(".env")
    if env_path.is_file():
        load_dotenv(env_path, override=False)
    elif args.env_file:
        log.warning("Env file %s not found - continuing without it.", env_path)

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and sys.platform.startswith("linux"):
        raise ConfigError("No display found. A visible browser window is required (headless mode was removed).")

    # --- link (not needed for --login)
    raw_url = args.url_opt or args.url or _pick(None, "MEETING_URL")
    if not raw_url and not args.login:
        raw_url = _ask("Teams meeting link")
    if not raw_url and not args.login:
        raise ConfigError("No meeting link. Pass it as an argument, set MEETING_URL, or run interactively.")
    url = normalize_url(raw_url) if raw_url else None

    # --- when
    join_at = "now" if (args.now or args.login) else _pick(args.join_at, "JOIN_AT")
    window = None if (args.now or args.login) else _pick(args.window, "JOIN_WINDOW")
    if args.join_at and not args.window:
        window = None  # explicit CLI choice beats a value sitting in .env
    elif args.window and not args.join_at:
        join_at = None
    if join_at and window:
        log.warning("Both a join time and a window were given - using the window.")
        join_at = None
    if not join_at and not window:
        answer = _ask("When to join? (14:30 | 09:55-10:05 | +10m | Enter = now)")
        if answer and WINDOW_RE.match(answer):
            window = answer
        else:
            join_at = answer or "now"

    name = " ".join((_pick(args.name, "DISPLAY_NAME", "Guest")).split())[:64]
    profile_dir = _pick(args.profile_dir, "PROFILE_DIR")
    try:
        browser = normalize_browser(_pick(args.browser, "BROWSER") or _pick(None, "BROWSER_CHANNEL"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from None

    return Settings(
        url=url,
        name=name,
        join_at=join_at,
        window=window,
        tz=_resolve_tz(_pick(args.timezone, "TIMEZONE")),
        screenshot_dir=Path(_pick(args.screenshot_dir, "SCREENSHOT_DIR", "screenshots")).expanduser(),
        lobby_timeout_min=_as_num(_pick(args.lobby_timeout, "LOBBY_TIMEOUT_MINUTES"), "LOBBY_TIMEOUT_MINUTES", 30.0, minimum=0.1),
        stay_min=_as_num(_pick(args.stay_minutes, "STAY_MINUTES"), "STAY_MINUTES", 120.0, minimum=0),
        screenshot_interval_min=_as_num(_pick(args.screenshot_interval, "SCREENSHOT_INTERVAL_MINUTES"), "SCREENSHOT_INTERVAL_MINUTES", 20.0, minimum=0),
        leave_after_screenshot=_as_bool(_pick(args.leave_after_screenshot, "LEAVE_AFTER_SCREENSHOT"), "LEAVE_AFTER_SCREENSHOT", False),
        max_attempts=_as_num(_pick(args.max_attempts, "MAX_ATTEMPTS"), "MAX_ATTEMPTS", 3, cast=int, minimum=1),
        grace_min=_as_num(_pick(args.grace_minutes, "GRACE_MINUTES"), "GRACE_MINUTES", 15.0, minimum=0),
        roll_over=_as_bool(_pick(args.roll_over, "ROLL_OVER"), "ROLL_OVER", False),
        browser=browser,
        profile=_pick(args.profile, "BROWSER_PROFILE"),
        refresh_profile=args.refresh_profile,
        profile_store=Path(_pick(args.profile_store, "PROFILE_STORE") or default_store()).expanduser().resolve(),
        profile_dir=Path(profile_dir).expanduser() if profile_dir else None,
        require_login=_as_bool(_pick(args.require_login, "REQUIRE_LOGIN"), "REQUIRE_LOGIN", True),
        preflight=_as_bool(_pick(args.preflight, "PREFLIGHT"), "PREFLIGHT", True),
        no_sandbox=_as_bool(_pick(args.no_sandbox, "NO_SANDBOX"), "NO_SANDBOX", False),
        login_only=args.login,
        dry_run=args.dry_run,
    )
