"""Drives the Teams web app in a real Chrome/Edge/Brave/Firefox window that stays signed in to YOUR account.

Why a dedicated profile? Since Chrome 136, Chrome refuses to be automated when it runs on its *default* profile.
So the script uses its own copy of a profile of yours (default <project>/browser-profiles/<browser>). You sign in once
(`teams-join --login`) and the session is remembered, exactly like in your normal browser.
"""
from __future__ import annotations

import logging
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, expect, sync_playwright
from playwright.sync_api import Error as PWError

from .browsers import brave_executable, candidate_order, is_installed
from .config import Settings, web_join_urls
from . import shutdown
from .errors import FatalJoinError, JoinError
from .profiles import prepare_profile

log = logging.getLogger("teams_auto_join")


def rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


# --------------------------------------------------------------------------- selectors / text patterns
GUEST_NAME = '[data-tid="prejoin-display-name-input"], input[placeholder="Type your name"]'
JOIN_BTN = '[data-tid="prejoin-join-button"], button:has-text("Join now")'
HANGUP = '#hangup-button, [data-tid="hangup-main-btn"], [data-tid="call-hangup"]'
TEAMS_HOSTS = ("teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft", "teams.microsoft.us")

LAUNCHER_RX = rx(r"continue on this browser|join on the web|use the web app")
DISMISS_RX = rx(r"^(got it|dismiss|accept|accept all|ok|continue without audio or video|maybe later|not now|no thanks)$")
MIC_ON_RX = rx(r"^(mute|turn off (the )?mic)")  # in-call button label while the mic is LIVE
CAM_ON_RX = rx(r"^turn (the )?camera off|^stop (my )?video")  # in-call label while the camera is LIVE

LOBBY_RX = rx(r"let you in|know you.re waiting|waiting for (someone|the (meeting )?organi[sz]er)|meeting (hasn.t|has not) started")
CAPTCHA_RX = rx(r"captcha|verify (that )?you.re (a )?human|prove you.re not a robot|enter the characters")
ENDED_RX = rx(r"meeting (has )?ended|the meeting is over|you.ve been removed|you were removed|you left the meeting|you.ve left the meeting")

BROWSER_ARGS = [
    "--use-fake-device-for-media-stream",  # virtual mic/webcam: your real devices are never touched
    "--use-fake-ui-for-media-stream",  # auto-accept the browser's mic/camera permission bubble
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--autoplay-policy=no-user-gesture-required",
]

# Firefox has no command-line flags for this; the equivalent are prefs (virtual mic/cam, no permission prompt)
FIREFOX_PREFS = {
    "media.navigator.streams.fake": True,
    "media.navigator.permission.disabled": True,
    "permissions.default.microphone": 1,
    "permissions.default.camera": 1,
    "media.autoplay.default": 0,
    "media.autoplay.blocking_policy": 0,
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.page": 0,  # don't reopen the tabs of the copied profile
    "browser.sessionstore.resume_from_crash": False,
    "datareporting.policy.dataSubmissionEnabled": False,
}

# (code, fatal?, human message, pattern) - first match wins
FAILURES: list[tuple[str, bool, str, re.Pattern]] = [
    ("invalid", True, "The meeting link is invalid, expired or the meeting no longer exists - get a fresh link.",
     rx(r"couldn.t find (that|this) meeting|meeting (link )?(is )?(not valid|invalid)|link (has )?expired|no longer (available|valid)|this meeting (has )?(ended|expired)")),
    ("denied", True, "The organizer/lobby denied or removed the request to join.",
     rx(r"denied access|request (was )?(denied|declined)|declined your request|(you|you.ve) (were|been) removed|removed (you )?from the meeting")),
    ("full", True, "The meeting is full.",
     rx(r"meeting is full|maximum number of (participants|attendees)|reached .{0,30}capacity")),
    ("no-access", True, "Your account is not allowed into this meeting (wrong organization/account?). Sign in with the right account via --login.",
     rx(r"you don.t have (access|permission)|isn.t available to (people|users) outside|not allowed to join|only (people|users).{0,40}(can|may) join")),
    ("snag", False, "Teams reported a temporary error (will retry).",
     rx(r"we hit a snag|something went wrong|try again later")),
]


def classify(text: str):
    for code, fatal, message, pattern in FAILURES:
        if pattern.search(text):
            return code, fatal, message
    return None


# --------------------------------------------------------------------------- small helpers
def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)  # innerText = only what is actually visible
    except PWError:
        return ""


def _visible(loc) -> bool:
    try:
        return loc.first.is_visible()
    except PWError:
        return False


def _host(page: Page) -> str:
    try:
        return (urlparse(page.url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _on_teams(page: Page) -> bool:
    h = _host(page)
    return any(h == t or h.endswith("." + t) for t in TEAMS_HOSTS)


def _on_login_page(page: Page) -> bool:
    """Any real host that is not Teams = Microsoft / company SSO sign-in, account picker, MFA..."""
    h = _host(page)
    return bool(h) and not _on_teams(page)


def _prejoin_ready(page: Page) -> bool:
    return _visible(page.locator(JOIN_BTN)) or _visible(page.locator(GUEST_NAME))


def _in_call(page: Page) -> bool:
    return _visible(page.locator(HANGUP)) or _visible(page.get_by_role("button", name=rx(r"^(leave|hang up)")))


def _click_launcher(page: Page) -> bool:
    for loc in (
        page.locator('[data-tid="joinOnWeb"]'),
        page.get_by_role("button", name=LAUNCHER_RX),
        page.get_by_role("link", name=LAUNCHER_RX),
    ):
        if not _visible(loc):
            continue
        try:
            loc.first.click(timeout=5000, force=True)
        except PWError:
            try:
                loc.first.dispatch_event("click")
            except PWError:
                continue
        log.info("Clicked 'Continue on this browser'. (If a native 'Open Microsoft Teams?' popup shows, press Cancel.)")
        return True
    return False


def _dismiss_popups(page: Page) -> None:
    try:
        buttons = page.get_by_role("button", name=DISMISS_RX)
        for i in range(min(buttons.count(), 3)):
            if buttons.nth(i).is_visible():
                buttons.nth(i).click(timeout=2000)
                time.sleep(0.5)
    except PWError:
        pass


# --------------------------------------------------------------------------- screenshots
def screenshot_path(base: Path, when: datetime, prefix: str = "") -> Path:
    """<base>/<YYYY-MM-DD>/<HH-MM-SS>.png  (dashes: ':' is illegal in Windows filenames)."""
    folder = base / when.strftime("%Y-%m-%d")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fallback = Path(tempfile.gettempdir()) / "teams-auto-join" / when.strftime("%Y-%m-%d")
        log.warning("Can't create %s (%s) - using %s instead.", folder, exc, fallback)
        fallback.mkdir(parents=True, exist_ok=True)
        folder = fallback
    stem = prefix + when.strftime("%H-%M-%S")
    path, n = folder / f"{stem}.png", 2
    while path.exists():  # two joins in the same second / re-runs never overwrite
        path = folder / f"{stem}_{n}.png"
        n += 1
    return path


def _save_screenshot(page: Page, path: Path) -> bool:
    for attempt in (1, 2):
        try:
            page.screenshot(path=str(path), timeout=15000)
            return True
        except PWError as exc:
            log.warning("Screenshot attempt %d failed: %s", attempt, str(exc).splitlines()[0])
            time.sleep(1)
    return False


def _failure_shot(page: Page | None, s: Settings, label: str) -> None:
    if page is None or page.is_closed():
        return
    path = screenshot_path(s.screenshot_dir, datetime.now(s.tz), prefix=f"failed_{label}_")
    if _save_screenshot(page, path):
        log.info("Saved debug screenshot: %s", path)


# --------------------------------------------------------------------------- mic / camera
def _toggle(page: Page, tid: str, name_rx: re.Pattern):
    for loc in (
        page.locator(f'[data-tid="{tid}"]'),
        page.get_by_role("switch", name=name_rx),
        page.get_by_role("checkbox", name=name_rx),
    ):
        try:
            if loc.count() and loc.first.is_visible():
                return loc.first
        except PWError:
            pass
    return None


def _is_on(el) -> bool | None:
    try:
        for attr in ("aria-checked", "aria-pressed"):
            val = el.get_attribute(attr)
            if val in ("true", "false"):
                return val == "true"
        inner = el.locator("input[type=checkbox]")
        if inner.count():
            return inner.first.is_checked()
    except PWError:
        pass
    return None


def _ensure_off(page: Page, label: str, tid: str, name_rx: re.Pattern) -> bool | None:
    """True = confirmed off, False = still on, None = could not determine."""
    for _ in range(3):
        el = _toggle(page, tid, name_rx)
        if el is None:
            log.warning("%s toggle not found on the pre-join screen.", label.capitalize())
            return None
        state = _is_on(el)
        if state is False:
            log.info("%s confirmed OFF.", label.capitalize())
            return True
        if state is None:
            return None
        el.click()
        time.sleep(0.8)
    return False


_MUTE_FIXES = {"microphone": 0, "camera": 0}  # corrective clicks per device in this attempt
MAX_MUTE_FIXES = 2  # never keep toggling a button back and forth


def _enforce_muted_in_call(page: Page) -> None:
    """Belt and braces: after joining, make sure mic and camera are really off (at most MAX_MUTE_FIXES clicks each)."""
    for label, pattern in (("microphone", MIC_ON_RX), ("camera", CAM_ON_RX)):
        if _MUTE_FIXES[label] >= MAX_MUTE_FIXES:
            continue
        try:
            btn = page.get_by_role("button", name=pattern).first
            if btn.is_visible():
                name = (btn.get_attribute("aria-label") or btn.inner_text() or "").strip()[:60]
                btn.click(timeout=3000)
                _MUTE_FIXES[label] += 1
                log.warning("%s looked live inside the call (button %r) - switched it off.", label.capitalize(), name)
        except PWError:
            pass


# --------------------------------------------------------------------------- sign-in
def _sign_in_button_visible(page: Page) -> bool:
    """The Teams landing page (still on a Teams host!) shows a 'Sign in' button when nobody is signed in."""
    return _visible(page.get_by_role("button", name=rx(r"^sign in$"))) or _visible(page.get_by_role("link", name=rx(r"^sign in$")))


def _wait_for_enter_or_close(page: Page) -> None:
    """--login: the user signs in at their own pace and tells us when they're done (no guessing)."""
    import threading

    done = threading.Event()

    def reader() -> None:
        try:
            sys.stdin.readline()
        except Exception:  # noqa: BLE001
            pass
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    print("\n>>> Sign in to Teams in the browser window now (choose 'Yes' on 'Stay signed in?').")
    print(">>> When you can see your Teams chats/calendar, come back here and press ENTER.\n", flush=True)
    while not done.is_set():
        try:
            page.wait_for_timeout(500)
        except PWError:
            log.warning("Browser window was closed - assuming sign-in is finished.")
            return
        if page.is_closed():
            log.warning("Browser window was closed - assuming sign-in is finished.")
            return


def _wait_signed_in(page: Page, wait_min: float) -> None:
    """Signed in = we stayed on a Teams domain for 10 s (a signed-out session gets bounced to a login host)."""
    deadline = time.monotonic() + wait_min * 60
    last_login_seen = time.monotonic()
    announced = False
    while True:
        if page.is_closed():
            raise FatalJoinError("The browser window was closed before sign-in finished.")
        if _on_login_page(page) or (_on_teams(page) and _sign_in_button_visible(page)):
            last_login_seen = time.monotonic()
            if not announced:
                log.warning("Please sign in to Teams in the browser window (waiting up to %.0f min)...", wait_min)
                announced = True
        elif _on_teams(page) and time.monotonic() - last_login_seen > 10:
            return
        if time.monotonic() > deadline:
            raise FatalJoinError("Sign-in did not finish in time. Run:  uv run teams-join --login")
        time.sleep(1)


# --------------------------------------------------------------------------- driver / cleanup
@contextmanager
def _shielded_from_ctrl_c():
    """Child processes started inside this block ignore Ctrl+C, while this process keeps handling it.

    Playwright's driver is a child process in our console's process group, so a Ctrl+C used to kill it too -
    and then nothing could click 'Leave' or close the browser. Both OS mechanisms below are *inherited* by children.
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCtrlHandler(None, True)  # "ignore Ctrl+C", inherited by child processes
        except Exception:  # noqa: BLE001
            yield
            return
        try:
            yield
        finally:
            kernel32.SetConsoleCtrlHandler(None, False)  # we handle it again
    else:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)  # an ignored signal stays ignored after exec()
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous)


@contextmanager
def _playwright():
    manager = sync_playwright()
    with _shielded_from_ctrl_c():
        pw = manager.start()  # this is where the driver process is spawned
    try:
        yield pw
    finally:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001 - driver already gone: nothing left to clean up
            pass


def _safe_close(ctx: BrowserContext | None) -> None:
    """Close the browser; never raise (the driver connection may already be gone)."""
    if ctx is None:
        return
    try:
        ctx.close()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- browser management
def _install_playwright_browser(name: str) -> None:
    log.info("Installing Playwright %s (one-time download)...", name)
    if subprocess.run([sys.executable, "-m", "playwright", "install", name]).returncode != 0:
        raise FatalJoinError(f"Automatic browser install failed. Run: uv run playwright install {name}")


def _resolve_profile(s: Settings, browser: str) -> tuple[Path, str | None, str]:
    """-> (user-data folder, --profile-directory value, human description)."""
    if s.profile_dir is not None:  # advanced: exact folder, used as-is
        return s.profile_dir, None, str(s.profile_dir)
    prepared = prepare_profile(s.profile_store, browser, s.profile, s.refresh_profile, always_ask=s.login_only)
    if prepared:
        return prepared.user_data_dir, prepared.profile_arg, prepared.description
    fresh = s.profile_store / f"{browser}-fresh"  # browser never used before: empty dedicated profile
    return fresh, None, str(fresh)


def _launch_one(pw, browser: str, user_data: Path, profile_arg: str | None, no_sandbox: bool = False) -> BrowserContext:
    """Start one specific browser. Raises PWError if it is missing or cannot start."""
    common = dict(user_data_dir=str(user_data), headless=False, no_viewport=True, locale="en-US")  # text patterns assume English
    if browser == "firefox":
        # Playwright can only drive its own patched Firefox build, not the Firefox installed on the system.
        return pw.firefox.launch_persistent_context(firefox_user_prefs=FIREFOX_PREFS, **common)
    args = list(BROWSER_ARGS)
    if profile_arg:
        args.append(f"--profile-directory={profile_arg}")
    kwargs = dict(
        common,
        permissions=["microphone", "camera"],  # virtual devices only; both are switched OFF before joining
        args=args,
        # keep the real OS keychain/password store so the cookies copied from your profile can be decrypted
        ignore_default_args=["--enable-automation", "--password-store=basic", "--use-mock-keychain"],
        # Playwright adds --no-sandbox unless told otherwise, which makes the browser show a red
        # "unsupported command-line flag: --no-sandbox" banner. Keep the real sandbox on.
        chromium_sandbox=not no_sandbox,
    )
    if browser == "brave":
        exe = brave_executable()
        if not exe:
            raise PWError("Executable doesn't exist: Brave is not installed")
        kwargs["executable_path"] = exe
    elif browser in ("chrome", "msedge"):
        kwargs["channel"] = browser
    return pw.chromium.launch_persistent_context(**kwargs)


def _is_blank_tab(url: str) -> bool:
    u = (url or "").lower()
    return u == "" or u.startswith(("about:", "chrome://", "brave://", "edge://", "chrome-untrusted://", "chrome-search://"))


def _tidy_tabs(ctx: BrowserContext, settle_s: float = 3.0) -> None:
    """Close the blank / new-tab pages a browser opens by itself at startup, so only ONE tab is used.

    Playwright always opens an about:blank tab and the browser may add its own start page. Only blank tabs are
    closed, and only during the first few seconds, so real pop-ups (e.g. the Microsoft sign-in window) are never touched.
    """
    main = None
    deadline = time.monotonic() + settle_s
    while True:
        pages = [p for p in ctx.pages if not p.is_closed()]
        if pages:
            main = main if main in pages else pages[0]
            for extra in pages:
                if extra is not main and _is_blank_tab(extra.url):
                    try:
                        extra.close()
                    except PWError:
                        pass
        if time.monotonic() >= deadline:
            return
        try:
            (main.wait_for_timeout(500) if main else time.sleep(0.5))  # lets Playwright receive new-tab events
        except PWError:
            return


def _launch_context(pw, s: Settings) -> BrowserContext:
    problems: list[str] = []
    for browser in candidate_order(s.browser):
        if browser != "firefox" and browser != "chromium" and is_installed(browser) is False:
            problems.append(f"{browser}: not installed")
            continue
        user_data, profile_arg, desc = _resolve_profile(s, browser)
        user_data.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                ctx = _launch_one(pw, browser, user_data, profile_arg, s.no_sandbox)
                _tidy_tabs(ctx)
                log.info("Browser: %s | profile: %s", browser, desc)
                return ctx
            except PWError as exc:
                msg = str(exc)
                missing = "Executable doesn't exist" in msg or "is not found" in msg or "not found at" in msg
                if not missing:
                    raise FatalJoinError(
                        f"Could not start {browser}. If a window using the automation profile is still open "
                        f"(or another teams-join is running), close it and retry. Details: {msg.splitlines()[0]}"
                    ) from exc
                # Firefox and plain Chromium come from Playwright itself: download them once if needed
                if browser in ("firefox", "chromium") and attempt == 1:
                    _install_playwright_browser(browser)
                    continue
                problems.append(f"{browser}: not installed")
                break
    raise FatalJoinError("No usable browser (" + "; ".join(problems) + "). Install Chrome, Brave or Edge, or run: uv run playwright install firefox")


def ensure_signed_in(s: Settings, wait_min: float = 5.0) -> None:
    """Preflight / --login: open Teams in the dedicated profile and make sure you're signed in."""
    log.info("Checking the browser and your Teams sign-in...")
    home = "https://teams.live.com/v2/" if (s.url and "teams.live.com" in s.url) else "https://teams.microsoft.com/v2/"
    with _playwright() as pw:
        ctx = _launch_context(pw, s)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(home, wait_until="domcontentloaded", timeout=60000)
            except PWError as exc:
                raise JoinError(f"Could not reach Teams (offline?): {str(exc).splitlines()[0]}") from exc
            if s.login_only and sys.stdin and sys.stdin.isatty():
                _wait_for_enter_or_close(page)
            else:
                _wait_signed_in(page, wait_min)
            log.info("Sign-in finished. The session is saved in the profile copy shown above.")
        finally:
            _safe_close(ctx)


# --------------------------------------------------------------------------- join phases
def _drive_to_prejoin(page: Page, s: Settings, budget: float) -> None:
    deadline = time.monotonic() + budget
    saw_login = login_logged = captcha_logged = False
    while time.monotonic() < deadline:
        if page.is_closed():
            raise JoinError("Browser page closed unexpectedly.")
        if _prejoin_ready(page):
            return
        if _on_login_page(page):
            saw_login = True
            if not login_logged:
                log.warning("Not signed in (or the session expired) - please sign in in the browser window (up to 5 min).")
                deadline = max(deadline, time.monotonic() + 390)
                login_logged = True
            time.sleep(1)
            continue
        text = _body_text(page)
        hit = classify(text)
        if hit:
            code, fatal, message = hit
            raise (FatalJoinError if fatal else JoinError)(f"[{code}] {message}")
        if CAPTCHA_RX.search(text):
            if not captcha_logged:
                log.warning("CAPTCHA shown - please solve it in the browser window (waiting up to 5 min).")
                deadline = max(deadline, time.monotonic() + 300)
                captcha_logged = True
        elif _click_launcher(page):
            time.sleep(2)
            continue
        _dismiss_popups(page)
        time.sleep(1)
    if saw_login:
        raise FatalJoinError("Sign-in did not finish. Run once:  uv run teams-join --login")
    raise JoinError("Timed out waiting for the pre-join screen.")


def _navigate_to_prejoin(page: Page, s: Settings) -> None:
    urls = web_join_urls(s.url)
    for i, url in enumerate(urls):
        last = i == len(urls) - 1
        log.info("Opening the meeting in the Teams web app%s...", "" if i == 0 else " (original link)")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except PWError as exc:
            log.warning("Navigation reported: %s", str(exc).splitlines()[0])
            if last and page.url in ("", "about:blank"):
                raise JoinError("Could not load the link (network down?).") from exc
        try:
            _drive_to_prejoin(page, s, budget=150 if last else 60)
            return
        except FatalJoinError:
            raise
        except JoinError as exc:
            if last:
                raise
            log.warning("Direct web link did not work (%s) - trying the original link.", exc)
    raise JoinError("Could not reach the pre-join screen.")


def _prepare_prejoin(page: Page, s: Settings) -> None:
    guest = page.locator(GUEST_NAME).first
    if _visible(guest):
        if s.require_login:
            raise FatalJoinError(
                "Teams is asking for a guest name, so this browser profile is NOT signed in. "
                "Run once:  uv run teams-join --login   (or set REQUIRE_LOGIN=false to join as a guest)"
            )
        guest.fill(s.name)
        if guest.input_value().strip() != s.name:
            guest.press("Control+A")
            guest.press_sequentially(s.name, delay=30)
        log.info("Joining as guest %r.", s.name)
    else:
        log.info("Joining with your signed-in account.")

    cam = _ensure_off(page, "camera", "toggle-video", rx(r"camera|video"))
    mic = _ensure_off(page, "microphone", "toggle-mute", rx(r"mic|audio"))
    for label, state in (("camera", cam), ("microphone", mic)):
        if state is False:  # virtual device, and it is force-muted again right after joining: not worth aborting
            log.warning("Could not switch the %s off on the pre-join screen - continuing (virtual device, muted after joining).", label)
    btn = page.locator(JOIN_BTN).first
    try:
        expect(btn).to_be_enabled(timeout=20000)
    except AssertionError:
        raise JoinError("The 'Join now' button never became enabled.") from None
    btn.click()
    log.info("Clicked 'Join now'.")


def _wait_for_admission(page: Page, timeout_min: float) -> None:
    started = time.monotonic()
    deadline = started + timeout_min * 60
    lobby_logged, reclicks = False, 0
    while True:
        if shutdown.requested.is_set():
            raise KeyboardInterrupt  # Ctrl+C while waiting in the lobby: cancel; cleanup in _attempt leaves/closes the browser
        if page.is_closed():
            raise JoinError("Browser page closed while joining.")
        text = _body_text(page)
        hit = classify(text)
        if hit:
            code, fatal, message = hit
            raise (FatalJoinError if fatal else JoinError)(f"[{code}] {message}")
        if LOBBY_RX.search(text):
            if not lobby_logged:
                log.info("In the lobby - waiting for the organizer to admit you (up to %.0f min)...", timeout_min)
                lobby_logged = True
        elif _in_call(page):
            return
        elif _prejoin_ready(page) and not lobby_logged and time.monotonic() - started > 20 and reclicks < 2:
            reclicks += 1
            log.warning("Still on the pre-join screen - clicking 'Join now' again.")
            try:
                page.locator(JOIN_BTN).first.click(timeout=5000)
            except PWError:
                pass
            started = time.monotonic()
        if time.monotonic() > deadline:
            raise FatalJoinError(f"Not admitted within {timeout_min:g} minutes - gave up.")
        time.sleep(1.5)


def _periodic_shot(page: Page, s: Settings, shots: list[Path]) -> None:
    path = screenshot_path(s.screenshot_dir, datetime.now(s.tz))
    if _save_screenshot(page, path):
        shots.append(path)
        log.info("Screenshot %d saved: %s", len(shots), path)
    else:
        log.error("COULD NOT save the periodic screenshot.")


def _stay(page: Page, s: Settings, shots: list[Path]) -> str:
    """Stay in the call; screenshot every SCREENSHOT_INTERVAL_MINUTES; leave after STAY_MINUTES (0 = until it ends)."""
    began = time.monotonic()
    end_at = began + s.stay_min * 60 if s.stay_min > 0 else None
    interval = s.screenshot_interval_min * 60
    next_shot = began + interval if interval > 0 else None
    last_shot = began  # the join screenshot was taken just before _stay()
    last_enforce, misses = began, 0
    while True:
        now = time.monotonic()
        if shutdown.requested.is_set():
            return "stopped by you (Ctrl+C)"
        if page.is_closed():
            return "browser window closed"
        if end_at and now >= end_at:
            if interval > 0 and now - last_shot > 60:  # closing screenshot, unless one was taken a moment ago
                _periodic_shot(page, s, shots)
            return "stay time elapsed"
        if ENDED_RX.search(_body_text(page)):
            return "meeting ended or you were removed"
        misses = 0 if _in_call(page) else misses + 1
        if misses >= 6:
            return "no longer in the call"
        if next_shot and now >= next_shot:
            _periodic_shot(page, s, shots)
            last_shot = time.monotonic()
            while next_shot <= last_shot:  # never "catch up" after a long sleep: one shot, then back on schedule
                next_shot += interval
            if end_at and next_shot >= end_at:
                next_shot = None  # the closing screenshot at the end covers the rest
        if time.monotonic() - last_enforce > 60:
            _enforce_muted_in_call(page)
            last_enforce = time.monotonic()
        time.sleep(2)


def _leave(page: Page) -> None:
    """Click Leave / Hang up and wait until we are really out of the call. Best effort, never raises."""
    try:
        if page.is_closed() or not _in_call(page):
            return
        hangup = page.locator(HANGUP)
        btn = hangup.first if _visible(hangup) else page.get_by_role("button", name=rx(r"^(leave|hang up)")).first
        btn.click(timeout=4000)
        time.sleep(1)
        confirm = page.get_by_role("button", name=rx(r"^leave$"))  # some Teams versions ask "Leave?"
        if _visible(confirm):
            confirm.first.click(timeout=3000)
        for _ in range(6):  # up to ~3 s for the call UI to disappear
            if not _in_call(page):
                log.info("Left the meeting.")
                return
            time.sleep(0.5)
        log.warning("Clicked Leave but the call screen is still showing - closing the browser instead.")
    except Exception:  # noqa: BLE001 - best effort during cleanup (driver/browser may already be gone)
        pass


@dataclass(frozen=True)
class JoinResult:
    joined_at: datetime
    screenshot: Path | None
    ended_reason: str
    shots: int = 1  # total screenshots taken (join + periodic)


def _attempt(s: Settings) -> JoinResult:
    with _playwright() as pw:
        ctx = _launch_context(pw, s)
        page = None
        in_meeting = False
        _MUTE_FIXES.update(microphone=0, camera=0)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(15000)

            _navigate_to_prejoin(page, s)
            _prepare_prejoin(page, s)
            shutdown.armed = True  # from now on the first Ctrl+C means "leave the meeting", handled by the loops below
            _wait_for_admission(page, s.lobby_timeout_min)

            in_meeting = True
            joined_at = datetime.now(s.tz)
            log.info("You're in the meeting (%s).", joined_at.strftime("%H:%M:%S"))
            time.sleep(3)  # let the call UI settle
            _dismiss_popups(page)
            _enforce_muted_in_call(page)

            path = screenshot_path(s.screenshot_dir, joined_at)
            shot = path if _save_screenshot(page, path) else None
            if shot:
                log.info("Screenshot saved: %s", shot)
            else:
                log.error("COULD NOT save the screenshot.")

            shots: list[Path] = [shot] if shot else []
            if s.leave_after_screenshot:
                return JoinResult(joined_at, shot, "leave-after-screenshot requested", len(shots))
            log.info("Staying in the meeting for %s, screenshot every %s. Press Ctrl+C once to leave gracefully.",
                     f"{s.stay_min:g} min" if s.stay_min else "as long as it lasts",
                     f"{s.screenshot_interval_min:g} min" if s.screenshot_interval_min else "- (only at join)")
            try:
                reason = _stay(page, s, shots)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - screenshots are already safe; don't turn a success into a failure
                reason = f"error while staying: {str(exc).splitlines()[0]}"
            return JoinResult(joined_at, shot, reason, len(shots))
        except KeyboardInterrupt:
            raise
        except Exception:
            if not in_meeting:
                _failure_shot(page, s, "join")
            raise
        finally:
            shutdown.armed = False  # a further Ctrl+C during cleanup = hard stop
            if page is not None:
                _leave(page)
            _safe_close(ctx)


def run_join(s: Settings) -> JoinResult:
    last: Exception | None = None
    for attempt in range(1, s.max_attempts + 1):
        log.info("Join attempt %d/%d", attempt, s.max_attempts)
        try:
            return _attempt(s)
        except FatalJoinError:
            raise
        except (JoinError, PWError) as exc:
            last = exc
            log.warning("Attempt %d failed: %s", attempt, str(exc).splitlines()[0])
            if attempt < s.max_attempts:
                delay = min(60, 10 * attempt)
                log.info("Retrying in %d s...", delay)
                time.sleep(delay)
    raise JoinError(f"All {s.max_attempts} attempts failed. Last error: {last}")
