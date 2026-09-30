"""The browser's own logic: crash backoff, memory settings, status and /proc.

The browser imports PyGObject, which only exists on the device's distro
Python — not in the venv and not on a dev machine. So `gi` is stubbed with
just enough for the module to import, and the tests stick to what doesn't need
a real WebKit: the arithmetic, the order of calls, and file handling. Whether
WebKit actually honours those calls is only provable on a device.
"""

import importlib
import sys
import types

import pytest


class Recorder:
    """Stands in for a WebKit object; records every method call made on it."""

    def __init__(self, calls, name):
        self._calls = calls
        self._name = name

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self._calls.append((f"{self._name}.{method}", args, kwargs))

        return call


def fake_gi():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *args: None
    repository = types.ModuleType("gi.repository")
    repository.GLib = types.SimpleNamespace(
        SOURCE_REMOVE=False, SOURCE_CONTINUE=True, PRIORITY_DEFAULT=0
    )
    repository.Gtk = types.SimpleNamespace(
        ApplicationWindow=type("ApplicationWindow", (), {}), Application=object
    )
    repository.WebKit = types.SimpleNamespace()
    gi.repository = repository
    return {"gi": gi, "gi.repository": repository}


@pytest.fixture(scope="module")
def browser():
    saved = {name: sys.modules.get(name) for name in ("gi", "gi.repository", "hmi_engine.browser")}
    sys.modules.update(fake_gi())
    sys.modules.pop("hmi_engine.browser", None)
    try:
        yield importlib.import_module("hmi_engine.browser")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture
def webkit(browser, monkeypatch):
    """A fake WebKit that logs, in order, every call the browser makes into it."""
    calls = []

    def new_settings():
        calls.append(("MemoryPressureSettings.new", (), {}))
        return Recorder(calls, "settings")

    def web_context(**kwargs):
        calls.append(("WebContext", (), kwargs))
        return "context"

    fake = types.SimpleNamespace(
        MemoryPressureSettings=types.SimpleNamespace(new=new_settings),
        NetworkSession=Recorder(calls, "NetworkSession"),
        WebContext=web_context,
    )
    monkeypatch.setattr(browser, "WebKit", fake)
    return calls


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestCrashBackoff:
    def crash_loop(self, backoff, clock, count):
        """Crash `count` times, each right after the previous reload."""
        delays = []
        for _ in range(count):
            delay = backoff.terminated()
            delays.append(delay)
            clock.t += delay + 2  # the reload, then a couple of seconds before it dies again
        return delays

    def test_a_single_crash_reloads_after_a_second(self, browser):
        assert browser.CrashBackoff(Clock()).terminated() == 1.0

    def test_a_crash_loop_backs_off_to_a_minute(self, browser):
        clock = Clock()
        backoff = browser.CrashBackoff(clock)
        assert self.crash_loop(backoff, clock, 9) == [1, 1, 5, 10, 20, 40, 60, 60, 60]

    def test_the_backoff_starts_from_retry_seconds(self, browser):
        clock = Clock()
        delays = self.crash_loop(browser.CrashBackoff(clock), clock, 3)
        assert delays[2] == browser.RETRY_SECONDS

    def test_crashes_spread_out_are_each_an_accident(self, browser):
        # Three inside five minutes is a loop; three across more than that isn't.
        clock = Clock()
        backoff = browser.CrashBackoff(clock)
        delays = []
        for _ in range(4):
            delays.append(backoff.terminated())
            clock.t += 3 * 60
        assert delays == [1, 1, 1, 1]

    def test_staying_up_resets_the_backoff(self, browser):
        clock = Clock()
        backoff = browser.CrashBackoff(clock)
        self.crash_loop(backoff, clock, 7)

        backoff.loaded()
        clock.t += 10 * 60
        assert backoff.terminated() == 1.0
        assert self.crash_loop(backoff, clock, 2) == [1, 5]

    def test_up_for_less_than_ten_minutes_keeps_it(self, browser):
        clock = Clock()
        backoff = browser.CrashBackoff(clock)
        self.crash_loop(backoff, clock, 7)

        backoff.loaded()
        clock.t += 9 * 60
        # Two crashes after a 9-minute run aren't three in five minutes, so the
        # first two reload quickly — but the loop, when it resumes, is at the cap.
        assert self.crash_loop(backoff, clock, 3) == [1, 1, 60]

    def test_only_the_first_load_since_a_crash_starts_the_clock(self, browser):
        # A SIGHUP reload after nine minutes up must not restart the ten.
        clock = Clock()
        backoff = browser.CrashBackoff(clock)
        self.crash_loop(backoff, clock, 7)
        backoff.loaded()
        clock.t += 9 * 60
        backoff.loaded()
        clock.t += 2 * 60
        assert backoff.terminated() == 1.0
        assert self.crash_loop(backoff, clock, 2) == [1, 5]

    def test_loading_without_a_crash_changes_nothing(self, browser):
        clock = Clock()
        backoff = browser.CrashBackoff(clock)
        backoff.loaded()
        backoff.loaded()
        assert backoff.terminated() == 1.0


class TestTerminationReason:
    def test_uses_the_enum_nick(self, browser):
        reason = types.SimpleNamespace(value_nick="exceeded-memory-limit")
        assert browser.termination_reason(reason) == "exceeded-memory-limit"

    def test_falls_back_to_str(self, browser):
        assert browser.termination_reason(3) == "3"


class TestMemoryPressure:
    def test_zero_leaves_webkit_alone(self, browser, webkit):
        assert browser.limited_web_context(0) is None
        assert webkit == []

    def test_negative_is_treated_as_unset(self, browser, webkit):
        assert browser.limited_web_context(-5) is None
        assert webkit == []

    def test_sets_the_limit_and_thresholds(self, browser, webkit):
        browser.memory_pressure_settings(512)
        calls = {name: args for name, args, _ in webkit}
        assert calls["settings.set_memory_limit"] == (512,)
        assert calls["settings.set_conservative_threshold"] == (0.5,)
        assert calls["settings.set_strict_threshold"] == (0.75,)
        assert calls["settings.set_kill_threshold"] == (1.25,)
        assert calls["settings.set_poll_interval"] == (30.0,)

    def test_thresholds_are_set_in_an_order_webkit_accepts(self, browser, webkit):
        # conservative < strict < kill must hold after every call, starting from
        # WebKit's defaults of 0.33 / 0.5 / 0 (0 = never kill).
        browser.memory_pressure_settings(512)
        current = {"conservative": 0.33, "strict": 0.5, "kill": 0.0}
        for name, args, _ in webkit:
            for key in current:
                if name == f"settings.set_{key}_threshold":
                    current[key] = args[0]
            assert 0 < current["conservative"] < current["strict"] < 1
            assert current["kill"] == 0 or current["kill"] > current["strict"]

    def test_the_kill_threshold_lands_under_the_devices_free_memory(self, browser):
        # A ~210 MiB page plus ~500 MB free: the device is out of RAM near 700.
        assert 512 * browser.KILL_THRESHOLD < 700
        assert 512 * browser.CONSERVATIVE_THRESHOLD > 220  # idle at steady state

    def test_network_session_is_configured_before_the_web_context(self, browser, webkit):
        context = browser.limited_web_context(512)
        order = [name for name, _, _ in webkit]
        assert order.index("NetworkSession.set_memory_pressure_settings") < order.index("WebContext")
        # And the web process gets the same settings, as a construct property.
        (_, _, kwargs) = webkit[order.index("WebContext")]
        assert set(kwargs) == {"memory_pressure_settings"}
        assert context == "context"


class TestArguments:
    def test_memory_limit_defaults_to_webkits_own(self, browser):
        args = browser.build_parser().parse_args(["https://x/"])
        assert args.memory_limit_mb == 0
        assert args.status_file is None

    def test_takes_a_memory_limit_and_status_file(self, browser):
        args = browser.build_parser().parse_args(
            ["https://x/", "--memory-limit-mb", "512", "--status-file", "/tmp/s"]
        )
        assert (args.memory_limit_mb, args.status_file) == (512, "/tmp/s")


class TestReportStatus:
    def test_prints_and_appends(self, browser, tmp_path, capsys):
        path = tmp_path / "status"
        browser.report_status("terminated crashed", str(path))
        browser.report_status("memory 212.5", str(path))
        assert capsys.readouterr().out.splitlines() == [
            "HMI-STATUS terminated crashed",
            "HMI-STATUS memory 212.5",
        ]
        assert path.read_text() == "HMI-STATUS terminated crashed\nHMI-STATUS memory 212.5\n"

    def test_a_multi_line_message_stays_one_line(self, browser, tmp_path):
        path = tmp_path / "status"
        browser.report_status("failed Could not\nconnect", str(path))
        assert path.read_text() == "HMI-STATUS failed Could not connect\n"

    def test_starts_afresh_past_the_size_cap(self, browser, tmp_path, monkeypatch):
        monkeypatch.setattr(browser, "STATUS_MAX_BYTES", 40)
        path = tmp_path / "status"
        for _ in range(2):
            browser.report_status("memory 200.0", str(path))
        assert path.read_text().count("\n") == 2  # 48 bytes: past the cap only now
        browser.report_status("memory 201.0", str(path))
        assert path.read_text() == "HMI-STATUS memory 201.0\n"

    def test_an_unwritable_file_does_not_raise(self, browser, tmp_path):
        browser.report_status("loaded", str(tmp_path / "missing-dir" / "status"))

    def test_no_file_just_prints(self, browser, capsys):
        browser.report_status("loaded", None)
        assert capsys.readouterr().out == "HMI-STATUS loaded\n"


def fake_proc(tmp_path, processes):
    """processes: pid -> (name, ppid, rss_kb or None)."""
    for pid, (name, ppid, rss_kb) in processes.items():
        entry = tmp_path / str(pid)
        entry.mkdir()
        (entry / "stat").write_text(f"{pid} ({name}) S {ppid} {pid} {pid} 0 -1 4194560 0")
        status = f"Name:\t{name}\nPPid:\t{ppid}\n"
        if rss_kb is not None:
            status += f"VmRSS:\t{rss_kb} kB\n"
        (entry / "status").write_text(status)
    (tmp_path / "self").mkdir()
    return tmp_path


class TestWebkitMemory:
    def test_finds_the_web_and_network_processes_under_the_browser(self, browser, tmp_path):
        proc = fake_proc(
            tmp_path,
            {
                1: ("python3", 0, 30000),
                50: ("python3", 1, 60000),  # the browser
                51: ("WebKitWebProces", 50, 217600),  # 212.5 MiB
                52: ("WebKitNetworkPr", 50, 40960),  # 40 MiB
                60: ("WebKitWebProces", 1, 999999),  # someone else's
            },
        )
        web, network = browser.webkit_memory(50, proc)
        assert web == pytest.approx(212.5)
        assert network == pytest.approx(40.0)

    def test_follows_grandchildren(self, browser, tmp_path):
        proc = fake_proc(
            tmp_path,
            {50: ("python3", 1, 1), 51: ("bwrap", 50, 1), 52: ("WebKitWebProces", 51, 102400)},
        )
        assert browser.webkit_memory(50, proc)[0] == pytest.approx(100.0)

    def test_none_between_a_crash_and_its_reload(self, browser, tmp_path):
        proc = fake_proc(tmp_path, {50: ("python3", 1, 1), 52: ("WebKitNetworkPr", 50, 1024)})
        assert browser.webkit_memory(50, proc) == (None, pytest.approx(1.0))

    def test_a_name_with_spaces_and_parens_does_not_confuse_it(self, browser, tmp_path):
        proc = fake_proc(
            tmp_path, {50: ("python3", 1, 1), 70: ("odd) (name", 50, 1), 71: ("WebKitWebProces", 70, 2048)}
        )
        assert browser.webkit_memory(50, proc)[0] == pytest.approx(2.0)

    def test_tolerates_processes_that_vanish_or_lack_rss(self, browser, tmp_path):
        proc = fake_proc(
            tmp_path,
            {50: ("python3", 1, 1), 51: ("WebKitWebProces", 50, None), 52: ("WebKitWebProces", 50, 1024)},
        )
        (proc / "52" / "status").unlink()
        assert browser.webkit_memory(50, proc) == (None, None)
