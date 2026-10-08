"""Graceful Ctrl+C.

While we are waiting to be admitted or sitting in the meeting ("armed"), the FIRST Ctrl+C (or `kill`) only sets a flag.
The loops in joiner.py notice it within a couple of seconds, click *Leave*, close the browser and exit with code 130.
A SECOND Ctrl+C - or a Ctrl+C at any other time - raises KeyboardInterrupt immediately (hard stop).
"""
from __future__ import annotations

import logging
import signal
import threading

log = logging.getLogger("teams_auto_join")

requested = threading.Event()  # set by the first Ctrl+C while armed
armed = False  # True only while the join / stay loops are polling `requested`


def _handler(signum, _frame) -> None:
    global armed
    if armed and not requested.is_set():
        requested.set()
        log.info("Ctrl+C received - leaving the meeting (press Ctrl+C again to force quit)...")
        return
    raise KeyboardInterrupt


def install() -> None:
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK = Ctrl+Break on Windows
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass
