"""The daily reload: when it is due, and that it fires once and re-arms.

Driven by a fake clock throughout — nothing here sleeps for real — because the
cases worth testing are the ones a real clock produces rarely: daylight-saving
nights, and a Doovit's clock being stepped by NTP after a boot with no RTC.
"""

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from hmi_engine import application as application_mod
from hmi_engine.app_config import HMIEngineConfig
from hmi_engine.application import HMIEngineApplication
from hmi_engine.schedule import (
    DailyReload,
    next_occurrence,
    parse_time_of_day,
    parse_timezone,
)

BRISBANE = ZoneInfo("Australia/Brisbane")  # UTC+10, no daylight saving
SYDNEY = ZoneInfo("Australia/Sydney")  # DST: forward 2026-10-04, back 2026-04-05


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestParseTimeOfDay:
    @pytest.mark.parametrize(
        "text, expected",
        [("00:00", time(0, 0)), ("23:59", time(23, 59)), ("7:05", time(7, 5)), (" 12:30 ", time(12, 30))],
    )
    def test_accepts_24_hour_times(self, text, expected):
        assert parse_time_of_day(text) == expected

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_blank_is_off(self, text):
        assert parse_time_of_day(text) is None

    @pytest.mark.parametrize("text", ["24:00", "12:60", "noon", "1230", "12:3", "12:30:00", "12:30pm", "-1:00"])
    def test_rejects_anything_else(self, text):
        with pytest.raises(ValueError, match="HH:MM"):
            parse_time_of_day(text)


class TestParseTimezone:
    @pytest.mark.parametrize("name", ["", None, "  "])
    def test_defaults_to_utc(self, name):
        assert parse_timezone(name) == ZoneInfo("UTC")

    def test_takes_an_iana_name(self):
        assert parse_timezone("Australia/Brisbane") == BRISBANE

    @pytest.mark.parametrize("name", ["Australia/Brisbnae", "AEST+10", "../../etc/passwd"])
    def test_rejects_unknown_names(self, name):
        with pytest.raises(ValueError, match="timezone"):
            parse_timezone(name)


class TestNextOccurrence:
    def test_later_today(self):
        # 10:00 in Brisbane; midnight UTC is 10:00 there, so 23:00 is today.
        due = next_occurrence(time(23, 0), BRISBANE, utc(2026, 10, 1, 0, 0))
        assert due == datetime(2026, 10, 1, 23, 0, tzinfo=BRISBANE)

    def test_tomorrow_once_today_has_passed(self):
        due = next_occurrence(time(0, 0), BRISBANE, utc(2026, 10, 1, 0, 0))
        assert due == datetime(2026, 10, 2, 0, 0, tzinfo=BRISBANE)

    def test_strictly_after_now(self):
        # Asked at exactly the due instant — as it is straight after firing —
        # the answer is tomorrow, not the same reload again.
        now = datetime(2026, 10, 2, 0, 0, tzinfo=BRISBANE)
        assert next_occurrence(time(0, 0), BRISBANE, now) == now + timedelta(days=1)

    def test_in_the_configured_zone_not_utc(self):
        due = next_occurrence(time(0, 0), ZoneInfo("UTC"), utc(2026, 10, 1, 0, 0))
        assert due == utc(2026, 10, 2, 0, 0)

    def test_a_time_in_the_spring_forward_gap_fires_once_an_hour_late(self):
        # 02:30 doesn't exist in Sydney on 4 October 2026 — 02:00 AEST jumps
        # to 03:00 AEDT. It is read under the old offset: 16:30 UTC, which the
        # wall clock calls 03:30 AEDT.
        now = datetime(2026, 10, 3, 12, 0, tzinfo=SYDNEY)
        due = next_occurrence(time(2, 30), SYDNEY, now)
        assert due.astimezone(UTC) == utc(2026, 10, 3, 16, 30)
        assert (due.hour, due.minute) == (3, 30)

        after = next_occurrence(time(2, 30), SYDNEY, due)
        assert after == datetime(2026, 10, 5, 2, 30, tzinfo=SYDNEY)

    def test_a_time_that_happens_twice_fires_only_on_the_first(self):
        # 5 April 2026: 03:00 AEDT goes back to 02:00 AEST, so 02:30 happens
        # at 15:30 UTC and again at 16:30 UTC.
        first = next_occurrence(time(2, 30), SYDNEY, datetime(2026, 4, 4, 12, 0, tzinfo=SYDNEY))
        assert first.astimezone(UTC) == utc(2026, 4, 4, 15, 30)

        # Straight after firing, and during the repeated hour, it's tomorrow.
        assert next_occurrence(time(2, 30), SYDNEY, first) == datetime(2026, 4, 6, 2, 30, tzinfo=SYDNEY)
        repeated_hour = utc(2026, 4, 4, 16, 10)
        assert next_occurrence(time(2, 30), SYDNEY, repeated_hour) == datetime(2026, 4, 6, 2, 30, tzinfo=SYDNEY)


class FakeClock:
    """A wall clock that only moves when slept on — or stepped, like NTP."""

    def __init__(self, start: datetime):
        self.t = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


def scheduler(clock, settings=("00:00", "Australia/Brisbane"), reloads=None):
    reloads = [] if reloads is None else reloads
    current = {"settings": settings}

    async def reload():
        reloads.append(clock.now())

    sched = DailyReload(lambda: current["settings"], reload, now=clock.now, sleep=clock.sleep)
    return sched, reloads, current


async def advance(sched, clock, until: datetime) -> None:
    """Run the check loop against the fake clock up to `until`."""
    while clock.t < until:
        await clock.sleep(await sched.step())


class TestDailyReload:
    def test_fires_at_the_time_and_rearms_for_the_next_day(self, caplog):
        caplog.set_level(logging.INFO)
        clock = FakeClock(datetime(2026, 10, 1, 21, 0, tzinfo=BRISBANE))
        sched, reloads, _ = scheduler(clock)

        asyncio.run(advance(sched, clock, datetime(2026, 10, 4, 1, 0, tzinfo=BRISBANE)))

        assert reloads == [
            datetime(2026, 10, 2, 0, 0, tzinfo=BRISBANE),
            datetime(2026, 10, 3, 0, 0, tzinfo=BRISBANE),
            datetime(2026, 10, 4, 0, 0, tzinfo=BRISBANE),
        ]
        assert sched.due == datetime(2026, 10, 5, 0, 0, tzinfo=BRISBANE)
        assert "Scheduled reload at 2026-10-02 00:00 AEST" in caplog.text
        assert "Scheduled reload at 2026-10-05 00:00 AEST" in caplog.text

    def test_never_sleeps_longer_than_a_check(self):
        clock = FakeClock(datetime(2026, 10, 1, 1, 0, tzinfo=BRISBANE))
        sched, _, _ = scheduler(clock)
        asyncio.run(advance(sched, clock, datetime(2026, 10, 2, 1, 0, tzinfo=BRISBANE)))
        assert max(clock.slept) <= DailyReload.CHECK_SECONDS

    def test_blank_is_off(self):
        clock = FakeClock(utc(2026, 10, 1))
        sched, reloads, _ = scheduler(clock, settings=("", "UTC"))
        asyncio.run(advance(sched, clock, utc(2026, 10, 3)))
        assert reloads == [] and sched.due is None

    def test_invalid_time_is_off_with_one_warning(self, caplog):
        clock = FakeClock(utc(2026, 10, 1))
        sched, reloads, _ = scheduler(clock, settings=("midnight", "UTC"))
        asyncio.run(advance(sched, clock, utc(2026, 10, 3)))
        assert reloads == []
        warnings = [r for r in caplog.records if "Scheduled reload is off" in r.message]
        assert len(warnings) == 1
        assert "'midnight' is not a 24-hour HH:MM time" in warnings[0].message

    def test_invalid_timezone_is_off(self, caplog):
        clock = FakeClock(utc(2026, 10, 1))
        sched, reloads, _ = scheduler(clock, settings=("00:00", "Brisbane"))
        asyncio.run(advance(sched, clock, utc(2026, 10, 3)))
        assert reloads == []
        assert "'Brisbane' is not a known timezone" in caplog.text

    def test_blank_timezone_is_utc(self):
        clock = FakeClock(utc(2026, 10, 1, 12))
        sched, reloads, _ = scheduler(clock, settings=("00:00", None))
        asyncio.run(advance(sched, clock, utc(2026, 10, 2, 1)))
        assert reloads == [utc(2026, 10, 2)]

    def test_rearms_when_the_config_changes(self):
        clock = FakeClock(datetime(2026, 10, 1, 21, 0, tzinfo=BRISBANE))
        sched, reloads, current = scheduler(clock)
        asyncio.run(advance(sched, clock, datetime(2026, 10, 1, 22, 0, tzinfo=BRISBANE)))
        assert sched.due == datetime(2026, 10, 2, 0, 0, tzinfo=BRISBANE)

        current["settings"] = ("23:00", "Australia/Brisbane")
        asyncio.run(advance(sched, clock, datetime(2026, 10, 2, 1, 0, tzinfo=BRISBANE)))
        # Re-armed for 23:00 and fired there; midnight was dropped with the old setting.
        assert reloads == [datetime(2026, 10, 1, 23, 0, tzinfo=BRISBANE)]

    def test_a_forward_clock_step_skips_rather_than_firing_at_a_random_hour(self, caplog):
        # Booted with a stale clock from a month ago; NTP then steps it to
        # mid-afternoon today, far past the midnight that was armed.
        clock = FakeClock(datetime(2026, 9, 1, 22, 0, tzinfo=BRISBANE))
        sched, reloads, _ = scheduler(clock)
        asyncio.run(sched.step())
        assert sched.due == datetime(2026, 9, 2, 0, 0, tzinfo=BRISBANE)

        clock.t = datetime(2026, 10, 1, 14, 0, tzinfo=BRISBANE)
        asyncio.run(sched.step())
        assert reloads == []
        assert sched.due == datetime(2026, 10, 2, 0, 0, tzinfo=BRISBANE)
        assert "jumped past the scheduled reload" in caplog.text

        asyncio.run(advance(sched, clock, datetime(2026, 10, 2, 0, 5, tzinfo=BRISBANE)))
        assert reloads == [datetime(2026, 10, 2, 0, 0, tzinfo=BRISBANE)]

    def test_a_backward_clock_step_rearms(self):
        clock = FakeClock(datetime(2026, 10, 1, 22, 0, tzinfo=BRISBANE))
        sched, reloads, _ = scheduler(clock)
        asyncio.run(sched.step())

        clock.t = datetime(2025, 1, 1, 12, 0, tzinfo=BRISBANE)
        asyncio.run(advance(sched, clock, datetime(2025, 1, 2, 0, 5, tzinfo=BRISBANE)))
        assert reloads == [datetime(2025, 1, 2, 0, 0, tzinfo=BRISBANE)]

    def test_a_failed_reload_still_rearms(self, caplog):
        clock = FakeClock(datetime(2026, 10, 1, 23, 59, tzinfo=BRISBANE))
        calls = []

        async def reload():
            calls.append(clock.now())
            raise RuntimeError("no browser")

        sched = DailyReload(lambda: ("00:00", "Australia/Brisbane"), reload, now=clock.now, sleep=clock.sleep)
        asyncio.run(advance(sched, clock, datetime(2026, 10, 3, 0, 5, tzinfo=BRISBANE)))
        assert len(calls) == 2
        assert "Scheduled reload failed" in caplog.text

    def test_run_survives_a_broken_settings_read(self, caplog):
        clock = FakeClock(utc(2026, 10, 1))
        reads = []

        def settings():
            reads.append(1)
            if len(reads) == 1:
                raise AttributeError("config not loaded yet")
            if len(reads) > 3:
                raise asyncio.CancelledError  # stop the loop
            return ("00:00", "UTC")

        async def reload():
            pass

        sched = DailyReload(settings, reload, now=clock.now, sleep=clock.sleep)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(sched.run())
        assert len(reads) == 4
        assert "Scheduled reload check failed" in caplog.text
        assert sched.due == utc(2026, 10, 2)


class TestApplicationWiring:
    def test_config_keys_match_their_attributes(self):
        # Runtime keys come from the display name — see CLAUDE.md.
        schema = HMIEngineConfig.to_schema()["properties"]
        assert schema["reload_at"]["default"] is None
        assert schema["timezone"]["default"] == "UTC"

        config = HMIEngineConfig()
        config._inject_deployment_config(
            {"reload_at": "01:30", "timezone": "Australia/Brisbane", "conflicting_services": []}
        )
        assert (config.reload_at.value, config.timezone.value) == ("01:30", "Australia/Brisbane")

    def test_scheduled_reload_signals_the_browser(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        app = object.__new__(HMIEngineApplication)
        app.session = type("S", (), {"running": True})()
        signalled = []
        monkeypatch.setattr(application_mod, "reload_page", lambda: signalled.append(1) or 1)

        asyncio.run(app._scheduled_reload())

        assert signalled == [1]
        assert "Reloaded the page on its daily schedule" in caplog.text
