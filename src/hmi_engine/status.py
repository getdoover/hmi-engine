"""What the browser tells the supervisor, and the tags that follow from it.

The browser reports on itself with `HMI-STATUS <kind> <detail>` lines. It
prints them, so they are in `docker logs`, but that stdout is inherited from
the compositor and deliberately never piped to this app: a pipe nobody drains
fills and blocks sway, and the display goes with it. So the browser also
appends each line to a small file in the runtime dir, and the app reads what
is new on every pass of its main loop. A file can't block anyone.

Kept free of pydoover, like `session` and `display`, so it tests on its own.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "HMI-STATUS "

#: A partial line longer than this is garbage, not a line still being written.
MAX_PARTIAL = 4096


@dataclass(frozen=True)
class Status:
    kind: str
    detail: str = ""


def parse_status(line: str) -> Status | None:
    """`HMI-STATUS terminated crashed` -> Status("terminated", "crashed")."""
    line = line.strip()
    if not line.startswith(PREFIX):
        return None
    kind, _, detail = line[len(PREFIX) :].strip().partition(" ")
    if not kind:
        return None
    return Status(kind, detail.strip())


class StatusFile:
    """Reads the lines the browser has appended since the last read.

    The browser starts the file afresh when it passes 64 KiB, and the app
    removes it before each new session so a previous browser's crashes are not
    counted twice. Either shows up here as the file being shorter than where
    reading left off, and reading starts again from the top.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._partial = b""

    def reset(self) -> None:
        """Forget the previous browser: remove its file and start from zero."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Could not clear %s: %s", self.path, exc)
        self._offset = 0
        self._partial = b""

    def read(self) -> list[Status]:
        """New status lines, oldest first. Never raises for a missing or odd file."""
        try:
            size = os.stat(self.path).st_size
            if size < self._offset:
                self._offset = 0
                self._partial = b""
            if size == self._offset:
                return []
            with open(self.path, "rb") as fh:
                fh.seek(self._offset)
                data = fh.read(size - self._offset)
        except OSError:
            return []  # not written yet, or gone between stat and open

        self._offset += len(data)
        lines = (self._partial + data).split(b"\n")
        self._partial = lines.pop()
        if len(self._partial) > MAX_PARTIAL:
            self._partial = b""

        found = []
        for raw in lines:
            status = parse_status(raw.decode(errors="replace"))
            if status is not None:
                found.append(status)
        return found


#: What each termination reason looks like on `last_error`. The keys are the
#: nicks of WebKit's `WebProcessTerminationReason`.
TERMINATION_MESSAGES = {
    "crashed": "The page's web process crashed; reloading",
    "exceeded-memory-limit": (
        "The page's web process went over its memory limit and was restarted; reloading"
    ),
    "terminated-by-api": "The page's web process was terminated; reloading",
}


class BrowserHealth:
    """Turns status lines into tag values.

    A web-process termination counts on `page_crashes` and is recorded, with
    its reason and time, on `last_page_crash`, which stays put so a crash at
    3 a.m. is still visible at 9. It also goes on `last_error` for as long as
    the page is down, and comes off again when the page next loads — so a
    crash loop is visible there, and one that healed isn't still reported as a
    current fault.
    """

    def __init__(self) -> None:
        self.crashes = 0
        self._crash_on_last_error = False

    def session_started(self) -> None:
        # Starting a session sets last_error itself; nothing of ours is on it.
        self._crash_on_last_error = False

    def update(self, status: Status, now: datetime) -> dict[str, object]:
        """The tag values `status` implies, by tag name. Empty if none."""
        if status.kind == "terminated":
            reason = status.detail or "unknown"
            self.crashes += 1
            self._crash_on_last_error = True
            return {
                "page_crashes": self.crashes,
                "last_page_crash": f"{reason} at {now:%Y-%m-%d %H:%M:%S %Z}".strip(),
                "last_error": TERMINATION_MESSAGES.get(
                    reason, f"The page's web process ended ({reason}); reloading"
                ),
            }
        if status.kind == "memory":
            try:
                mb = float(status.detail)
            except ValueError:
                return {}
            return {"page_memory_mb": round(mb, 1)} if math.isfinite(mb) and mb >= 0 else {}
        if status.kind == "loaded" and self._crash_on_last_error:
            self._crash_on_last_error = False
            return {"last_error": ""}
        return {}
