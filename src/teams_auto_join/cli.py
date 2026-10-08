"""Entry point: load settings -> plan the time -> preflight -> wait -> join."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import shutdown
from .config import load_settings
from .errors import ConfigError, FatalJoinError, JoinError
from .scheduler import plan_join, wait_until

log = logging.getLogger("teams_auto_join")


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    try:
        Path("logs").mkdir(exist_ok=True)
        file_handler = logging.FileHandler("logs/teams-auto-join.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
        log.addHandler(file_handler)
    except OSError:
        pass  # read-only directory: console logging is enough


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    shutdown.install()  # Ctrl+C / `kill`: leave the meeting cleanly, close the browser, exit 130
    try:
        settings = load_settings(argv)
        if settings.login_only:
            from .joiner import ensure_signed_in

            ensure_signed_in(settings, wait_min=10)
            log.info("All set - you can now run teams-join without --login.")
            return 0
        plan = plan_join(
            join_at=settings.join_at,
            window=settings.window,
            tz=settings.tz,
            grace_min=settings.grace_min,
            roll_over=settings.roll_over,
        )
        log.info("Meeting : %s", settings.url)
        log.info("Join at : %s  (%s)", plan.target.strftime("%Y-%m-%d %H:%M:%S %Z"), plan.description)
        log.info("Browser : %s | stay %s | screenshot every %s", settings.browser,
                 f"{settings.stay_min:g} min" if settings.stay_min else "until the meeting ends",
                 f"{settings.screenshot_interval_min:g} min" if settings.screenshot_interval_min else "- (only at join)")
        if settings.dry_run:
            log.info("Dry run - nothing was opened.")
            return 0

        from .joiner import ensure_signed_in, run_join  # imported late so --help / --dry-run stay instant

        if settings.preflight:
            ensure_signed_in(settings)
        wait_until(plan.target, settings.tz)
        result = run_join(settings)
        log.info("Done. Joined at %s | first screenshot: %s | %d screenshot(s) total | ended because: %s",
                 result.joined_at.strftime("%H:%M:%S"), result.screenshot or "none", result.shots, result.ended_reason)
        return 130 if shutdown.requested.is_set() else 0
    except ConfigError as exc:
        log.error("Config problem: %s", exc)
        return 2
    except FatalJoinError as exc:
        log.error("Could not join: %s", exc)
        return 1
    except JoinError as exc:
        log.error("Could not join: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted - bye.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
