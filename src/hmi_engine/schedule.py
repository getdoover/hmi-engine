"""Reload the page once a day, at a time nobody is standing at the panel.

`reload_minutes` reloads on a rolling interval, which lands wherever it lands —
often mid-shift, under someone's finger. A wall display that runs for weeks
wants its guard reload at a fixed hour instead, usually overnight.

Everything here is derived from the wall clock each time it is needed, never
from a duration measured once. A Doovit has no RTC: it boots with whatever time
it last saved, and NTP steps the clock later — sometimes by weeks. A reload
armed as "sleep 9 hours" before that step would fire at a random hour after
it; one re-derived from `now` every minute notices the step and re-aims.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "UTC"

_TIME_OF_DAY = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def parse_time_of_day(text: str | None) -> time | None:
    """`"HH:MM"`, 24-hour, into a `time`. Blank means no scheduled reload.

    Raises `ValueError` for anything else, so the caller can say what was
    wrong rather than silently reloading at some other hour.
    """
    text = (text or "").strip()
    if not text:
        return None
    match = _TIME_OF_DAY.fullmatch(text)
    if match is None:
        raise ValueError(f"{text!r} is not a 24-hour HH:MM time, e.g. 00:00 or 23:30")
    return time(int(match[1]), int(match[2]))


def parse_timezone(name: str | None) -> tzinfo:
    """An IANA zone name such as `Australia/Brisbane`. Blank means UTC.

    Raises `ValueError` for a name the zone database doesn't know.
    """
    name = (name or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"{name!r} is not a known timezone; use an IANA name such as Australia/Brisbane"
        ) from exc


def next_occurrence(at: time, tz: tzinfo, now: datetime) -> datetime:
    """The first moment strictly after `now` when the clock in `tz` reads `at`.

    Compared in UTC on purpose: two datetimes sharing a tzinfo compare by wall
    clock, which gets both daylight-saving edges wrong. On the night the clocks
    go forward a time in the gap (02:30 in Sydney) doesn't exist, and it is
    taken as the same instant under the old offset — an hour later by the new
    one, still once. On the night they go back a time that happens twice fires
    on the first pass only.

    The result is normalised so its wall time is the one a person would read
    off a clock at that instant, which is what gets logged.
    """
    now_utc = now.astimezone(UTC)
    today = now.astimezone(tz).date()
    for days in range(3):
        candidate = datetime.combine(today + timedelta(days=days), at, tzinfo=tz)
        instant = candidate.astimezone(UTC)
        if instant > now_utc:
            return instant.astimezone(tz)
    raise AssertionError("unreachable: a wall time recurs within two days")


def describe(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M %Z").strip()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DailyReload:
    """Arms a reload for the next configured time of day, fires it, re-arms.

    `settings` is read on every check rather than once, so a config change is
    picked up within `CHECK_SECONDS` without needing a subscription of its own
    (pydoover already injects deployment-config updates into the app's config,
    and a redeploy restarts the container anyway).
    """

    #: The longest single sleep. Bounds how late a config change or a clock
    #: step is noticed.
    CHECK_SECONDS = 60

    #: How far past its time a reload may still fire. A check lands at most
    #: `CHECK_SECONDS` late in normal running; being any further past it means
    #: the clock jumped over it, and the right answer is tomorrow — not a
    #: reload at whatever hour NTP happened to set.
    GRACE_SECONDS = 5 * 60

    #: Further ahead than this, the armed time can only be left over from a
    #: clock that has since stepped backwards. A day plus DST's extra hour is
    #: the most a real next occurrence can be away.
    MAX_AHEAD = timedelta(hours=26)

    def __init__(
        self,
        settings: Callable[[], tuple[str | None, str | None]],
        reload: Callable[[], Awaitable[None]],
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._reload = reload
        self._now = now
        self._sleep = sleep
        self._armed_for: tuple[str | None, str | None] | None = None
        self._rejected: tuple[str | None, str | None] | None = None
        #: When the next reload is due, or None while there is no schedule.
        self.due: datetime | None = None

    async def run(self) -> None:
        """Forever. Only cancellation ends it; a failed check is logged and retried."""
        while True:
            try:
                delay = await self.step()
            except Exception:  # noqa: BLE001 — the schedule must never take the app down
                log.exception("Scheduled reload check failed; trying again shortly")
                delay = self.CHECK_SECONDS
            await self._sleep(delay)

    async def step(self) -> float:
        """One check: arm, fire or re-aim as the clock says. Returns seconds to sleep."""
        settings = self._settings()
        schedule = self._parse(settings)
        if schedule is None:
            if self.due is not None:
                log.info("Scheduled reload turned off")
            self.due = None
            self._armed_for = None
            return self.CHECK_SECONDS

        at, tz = schedule
        now = self._now()
        if self.due is None or settings != self._armed_for:
            self._arm(at, tz, now, settings)
            return self._wait(now)

        remaining = (self.due - now).total_seconds()
        if remaining > self.MAX_AHEAD.total_seconds():
            log.warning("The clock went backwards; re-arming the scheduled reload")
            self._arm(at, tz, now, settings)
        elif remaining < -self.GRACE_SECONDS:
            log.warning(
                "The clock jumped past the scheduled reload at %s; skipping it",
                describe(self.due),
            )
            self._arm(at, tz, now, settings)
        elif remaining <= 0:
            try:
                await self._reload()
            except Exception:  # noqa: BLE001 — tomorrow's reload still stands
                log.exception("Scheduled reload failed")
            now = self._now()
            self._arm(at, tz, now, settings)
        return self._wait(now)

    def _arm(self, at: time, tz: tzinfo, now: datetime, settings) -> None:
        self.due = next_occurrence(at, tz, now)
        self._armed_for = settings
        log.info("Scheduled reload at %s", describe(self.due))

    def _wait(self, now: datetime) -> float:
        remaining = (self.due - now).total_seconds()
        return max(0.0, min(remaining, self.CHECK_SECONDS))

    def _parse(self, settings) -> tuple[time, tzinfo] | None:
        at_text, tz_text = settings
        try:
            at = parse_time_of_day(at_text)
            if at is None:
                return None
            tz = parse_timezone(tz_text)
        except ValueError as exc:
            # Once per distinct bad value: the check runs every minute.
            if settings != self._rejected:
                log.warning("Scheduled reload is off: %s", exc)
                self._rejected = settings
            return None
        self._rejected = None
        return at, tz
