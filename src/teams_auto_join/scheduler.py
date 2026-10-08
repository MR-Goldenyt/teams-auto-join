"""Work out *when* to join (exact time or random moment in a window) and wait for it."""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, tzinfo

from .errors import ConfigError

log = logging.getLogger("teams_auto_join")

CLOCK_FORMATS = ("%H:%M:%S", "%H:%M", "%I:%M:%S%p", "%I:%M%p", "%I:%M:%S %p", "%I:%M %p")
DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")
RELATIVE_RE = re.compile(r"^\+?\s*(?:in\s+)?(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs)$", re.I)
WINDOW_RE = re.compile(
    r"^\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?)\s*(?:-|–|—|\bto\b)\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?)\s*$",
    re.I,
)


@dataclass(frozen=True)
class Plan:
    target: datetime
    description: str


def parse_clock(text: str) -> dtime:
    t = text.strip().upper().replace(".", "")
    for fmt in CLOCK_FORMATS:
        try:
            return datetime.strptime(t, fmt).time()
        except ValueError:
            continue
    raise ConfigError(f"Can't read the time {text!r}. Use e.g. 14:30, 14:30:15 or 2:30pm.")


def _parse_when(text: str, tz: tzinfo, now: datetime) -> tuple[datetime | None, dtime | None]:
    """Returns (absolute datetime, None) or (None, clock-only time)."""
    s = text.strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz), None
        except ValueError:
            continue
    return None, parse_clock(s)


def _combine(day: date, t: dtime, tz: tzinfo) -> datetime:
    return datetime.combine(day, t).replace(tzinfo=tz)


def plan_join(
    *,
    join_at: str | None,
    window: str | None,
    tz: tzinfo,
    grace_min: float,
    roll_over: bool,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> Plan:
    rng = rng or random.Random()
    now = now or datetime.now(tz)
    grace = timedelta(minutes=grace_min)

    if window:
        m = WINDOW_RE.match(window)
        if not m:
            raise ConfigError(f"Window {window!r} is invalid. Use HH:MM-HH:MM, e.g. 09:55-10:05.")
        start = _combine(now.date(), parse_clock(m.group(1)), tz)
        end = _combine(now.date(), parse_clock(m.group(2)), tz)
        if end < start:  # overnight window, e.g. 23:55-00:05
            end += timedelta(days=1)
        if now > end:
            if now - end <= grace:
                return Plan(now, f"window {window} ended {int((now - end).total_seconds())}s ago (within grace) -> joining now")
            if not roll_over:
                raise ConfigError(
                    f"The window {window} already passed today. Pick a new time, or enable ROLL_OVER to use tomorrow."
                )
            start, end = start + timedelta(days=1), end + timedelta(days=1)
        if now > start:
            start = now  # window is already open: choose from what's left of it
        span = (end - start).total_seconds()
        target = start + timedelta(seconds=rng.uniform(0, max(span, 0)))
        return Plan(target.replace(microsecond=0), f"random moment inside window {window}")

    spec = (join_at or "now").strip()
    if spec.lower() in ("now", "asap", "immediately", ""):
        return Plan(now, "joining immediately")

    rel = RELATIVE_RE.match(spec)
    if rel:
        amount, unit = float(rel.group(1)), rel.group(2).lower()
        secs = amount * (1 if unit.startswith("s") else 3600 if unit.startswith("h") else 60)
        return Plan(now + timedelta(seconds=secs), f"{spec} from now")

    absolute, clock = _parse_when(spec, tz, now)
    target = absolute or _combine(now.date(), clock, tz)
    if target < now:
        late = now - target
        if late <= grace:
            return Plan(now, f"{spec} passed {int(late.total_seconds())}s ago (within grace) -> joining now")
        if absolute is None and roll_over:
            target += timedelta(days=1)
            return Plan(target, f"{spec} already passed today -> scheduled for tomorrow")
        raise ConfigError(
            f"{spec} is in the past ({int(late.total_seconds() // 60)} min ago). "
            "Give a future time, a full date ('2026-10-09 14:30'), or enable ROLL_OVER."
        )
    return Plan(target, f"exact time {spec}")


def wait_until(target: datetime, tz: tzinfo, log_every: float = 300.0) -> None:
    """Sleep until `target`. Re-checks the wall clock constantly, so laptop sleep/suspend can't oversleep it."""
    last_log = float("-inf")
    while True:
        remaining = (target - datetime.now(tz)).total_seconds()
        if remaining <= 0:
            if remaining < -60:
                log.warning("Woke up %.0f s after the planned time (system sleep?). Joining now.", -remaining)
            return
        if time.monotonic() - last_log >= log_every:
            h, rem = divmod(int(remaining), 3600)
            m, s = divmod(rem, 60)
            log.info("Waiting... %dh %02dm %02ds until %s", h, m, s, target.strftime("%Y-%m-%d %H:%M:%S %Z"))
            last_log = time.monotonic()
        time.sleep(1.0 if remaining < 10 else 5.0)
