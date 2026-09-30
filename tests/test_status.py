"""What the browser reports, and the tags the app sets from it.

The browser's `HMI-STATUS` lines reach the app through a file rather than a
pipe (see `status`), so these cover the reading as well as the meaning: a
line written in two halves, a file started afresh, a previous browser's leftovers.
"""

import asyncio
import shlex
from datetime import UTC, datetime

import pytest

from hmi_engine.app_config import HMIEngineConfig
from hmi_engine.app_tags import HMIEngineTags
from hmi_engine.application import HMIEngineApplication
from hmi_engine.session import STATUS_PATH
from hmi_engine.status import BrowserHealth, Status, StatusFile, parse_status

NOW = datetime(2026, 10, 1, 3, 12, 44, tzinfo=UTC)


class TestParseStatus:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("HMI-STATUS terminated crashed", Status("terminated", "crashed")),
            ("HMI-STATUS terminated exceeded-memory-limit\n", Status("terminated", "exceeded-memory-limit")),
            ("HMI-STATUS memory 212.5", Status("memory", "212.5")),
            ("HMI-STATUS loaded", Status("loaded", "")),
            ("HMI-STATUS failed Could not connect: Connection refused", Status("failed", "Could not connect: Connection refused")),
        ],
    )
    def test_reads_the_kind_and_detail(self, line, expected):
        assert parse_status(line) == expected

    @pytest.mark.parametrize("line", ["", "HMI-STATUS", "HMI-STATUS ", "CONSOLE LOG hello", "hmi-status loaded"])
    def test_ignores_anything_else(self, line):
        assert parse_status(line) is None


class TestStatusFile:
    def test_returns_only_what_is_new(self, tmp_path):
        path = tmp_path / "status"
        reader = StatusFile(path)
        assert reader.read() == []  # not written yet

        path.write_text("HMI-STATUS loaded\n")
        assert reader.read() == [Status("loaded")]
        assert reader.read() == []

        with path.open("a") as fh:
            fh.write("HMI-STATUS memory 200.0\nHMI-STATUS terminated crashed\n")
        assert reader.read() == [Status("memory", "200.0"), Status("terminated", "crashed")]

    def test_a_line_caught_half_written_is_read_whole_later(self, tmp_path):
        path = tmp_path / "status"
        reader = StatusFile(path)
        path.write_text("HMI-STATUS loaded\nHMI-STATUS termin")
        assert reader.read() == [Status("loaded")]
        with path.open("a") as fh:
            fh.write("ated crashed\n")
        assert reader.read() == [Status("terminated", "crashed")]

    def test_follows_the_file_being_started_afresh(self, tmp_path):
        path = tmp_path / "status"
        reader = StatusFile(path)
        path.write_text("HMI-STATUS memory 200.0\n" * 10)
        assert len(reader.read()) == 10

        path.write_text("HMI-STATUS memory 201.0\n")  # the browser's size cap
        assert reader.read() == [Status("memory", "201.0")]

    def test_reset_forgets_the_previous_browser(self, tmp_path):
        path = tmp_path / "status"
        path.write_text("HMI-STATUS terminated crashed\n")
        reader = StatusFile(path)
        reader.reset()
        assert not path.exists()
        assert reader.read() == []

        path.write_text("HMI-STATUS loaded\n")
        assert reader.read() == [Status("loaded")]

    def test_reset_without_a_file_is_fine(self, tmp_path):
        StatusFile(tmp_path / "nope" / "status").reset()

    def test_skips_noise_between_status_lines(self, tmp_path):
        path = tmp_path / "status"
        path.write_bytes(b"garbage\n\xff\xfe\nHMI-STATUS loaded\n")
        assert StatusFile(path).read() == [Status("loaded")]


class TestBrowserHealth:
    def test_a_crash_is_counted_recorded_and_reported(self):
        health = BrowserHealth()
        updates = health.update(Status("terminated", "exceeded-memory-limit"), NOW)
        assert updates == {
            "page_crashes": 1,
            "last_page_crash": "exceeded-memory-limit at 2026-10-01 03:12:44 UTC",
            "last_error": "The page's web process went over its memory limit and was restarted; reloading",
        }
        assert health.update(Status("terminated", "crashed"), NOW)["page_crashes"] == 2

    @pytest.mark.parametrize(
        "reason, message",
        [
            ("crashed", "The page's web process crashed; reloading"),
            ("terminated-by-api", "The page's web process was terminated; reloading"),
            ("something-new", "The page's web process ended (something-new); reloading"),
            ("", "The page's web process ended (unknown); reloading"),
        ],
    )
    def test_every_reason_reads_plainly(self, reason, message):
        assert BrowserHealth().update(Status("terminated", reason), NOW)["last_error"] == message

    def test_the_error_clears_once_the_page_is_back_but_the_record_stays(self):
        health = BrowserHealth()
        health.update(Status("terminated", "crashed"), NOW)
        assert health.update(Status("loaded"), NOW) == {"last_error": ""}
        # Only once: a later load must not clear an error someone else set.
        assert health.update(Status("loaded"), NOW) == {}

    def test_a_load_with_no_crash_before_it_touches_nothing(self):
        assert BrowserHealth().update(Status("loaded"), NOW) == {}

    def test_a_new_session_owns_last_error(self):
        health = BrowserHealth()
        health.update(Status("terminated", "crashed"), NOW)
        health.session_started()
        assert health.update(Status("loaded"), NOW) == {}

    def test_memory_goes_on_its_tag_as_float_mb(self):
        assert BrowserHealth().update(Status("memory", "212.46"), NOW) == {"page_memory_mb": 212.5}

    def test_rss_and_private_go_on_their_own_tags(self):
        assert BrowserHealth().update(Status("memory", "262.0 159.04"), NOW) == {
            "page_memory_mb": 262.0,
            "page_private_mb": 159.0,
        }

    def test_a_bad_private_reading_keeps_the_rss(self):
        assert BrowserHealth().update(Status("memory", "262.0 lots"), NOW) == {"page_memory_mb": 262.0}

    @pytest.mark.parametrize("detail", ["", "lots", "nan", "-3", "lots 159.0"])
    def test_a_bad_memory_reading_is_ignored(self, detail):
        assert BrowserHealth().update(Status("memory", detail), NOW) == {}

    def test_a_failed_load_is_left_to_the_logs(self):
        assert BrowserHealth().update(Status("failed", "Connection refused"), NOW) == {}


class FakeTag:
    def __init__(self, value=None):
        self.value = value

    async def set(self, value):
        self.value = value


class FakeTags:
    def __init__(self):
        for name in ("last_error", "page_crashes", "last_page_crash", "page_memory_mb", "page_private_mb", "showing"):
            setattr(self, name, FakeTag())


def configured(**values):
    config = HMIEngineConfig()
    config._inject_deployment_config({"conflicting_services": [], **values})
    return config


def app_with(config=None, status_path=None):
    app = object.__new__(HMIEngineApplication)
    app.config = config or configured()
    app.tags = FakeTags()
    app._browser_status = StatusFile(status_path or STATUS_PATH)
    app._health = BrowserHealth()
    return app


class TestApplicationWiring:
    def test_memory_limit_key_and_default(self):
        schema = HMIEngineConfig.to_schema()["properties"]
        assert schema["memory_limit_mb"]["default"] == 320
        assert schema["memory_limit_mb"]["minimum"] == 0
        assert configured().memory_limit_mb.value == 320

    def test_passes_the_memory_limit_to_the_browser(self):
        argv = shlex.split(app_with()._browser_command("https://x/"))
        assert argv[argv.index("--memory-limit-mb") + 1] == "320"

    def test_passes_a_configured_limit_as_whole_mb(self):
        argv = shlex.split(app_with(configured(memory_limit_mb=768.6))._browser_command("https://x/"))
        assert argv[argv.index("--memory-limit-mb") + 1] == "768"

    @pytest.mark.parametrize("limit", [0.0, 0.4, None])
    def test_no_flag_means_webkits_default(self, limit):
        argv = shlex.split(app_with(configured(memory_limit_mb=limit))._browser_command("https://x/"))
        assert "--memory-limit-mb" not in argv

    def test_tells_the_browser_where_to_report(self):
        argv = shlex.split(app_with()._browser_command("https://x/"))
        assert argv[argv.index("--status-file") + 1] == str(STATUS_PATH)

    def test_status_lines_become_tags(self, tmp_path):
        path = tmp_path / "status"
        app = app_with(status_path=path)
        path.write_text(
            "HMI-STATUS loaded\n"
            "HMI-STATUS memory 598.2 402.7\n"
            "HMI-STATUS terminated exceeded-memory-limit\n"
        )
        asyncio.run(app._read_browser_status())
        assert app.tags.page_crashes.value == 1
        assert app.tags.last_page_crash.value.startswith("exceeded-memory-limit at ")
        assert "memory limit" in app.tags.last_error.value
        assert app.tags.page_memory_mb.value == 598.2
        assert app.tags.page_private_mb.value == 402.7

        with path.open("a") as fh:
            fh.write("HMI-STATUS loaded\nHMI-STATUS memory 214.0 110.5\n")
        asyncio.run(app._read_browser_status())
        assert app.tags.last_error.value == ""
        assert app.tags.page_crashes.value == 1
        assert app.tags.page_memory_mb.value == 214.0
        assert app.tags.page_private_mb.value == 110.5

    def test_every_tag_it_sets_is_declared(self):
        declared = {name for name in vars(HMIEngineTags) if not name.startswith("_")}
        health = BrowserHealth()
        produced = set()
        for status in (Status("terminated", "crashed"), Status("memory", "1 2"), Status("loaded")):
            produced |= set(health.update(status, NOW))
        assert produced <= declared
