"""The generated compositor config is the contract between this app and sway."""

import signal

from hmi_engine import session as session_mod
from hmi_engine.display import Display, Mode
from hmi_engine.session import (
    browser_pids,
    build_sway_config,
    reload_page,
    session_environment,
)


def make(accelerated=False, dri_path="/usr/lib/dri"):
    return Display(
        connector="HDMI-A-1",
        device="/dev/dri/card1",
        modes=(Mode(1920, 1080),),
        accelerated=accelerated,
        dri_path=dri_path,
    )


class TestSwayConfig:
    def test_pins_the_output_mode(self):
        config = build_sway_config(make(), Mode(1280, 720, 60), 0, True, "hmi-browser url")
        assert "output HDMI-A-1 mode 1280x720@60Hz" in config

    def test_omits_the_mode_when_using_the_panels_preference(self):
        config = build_sway_config(make(), None, 0, True, "hmi-browser url")
        assert "output HDMI-A-1 transform normal" in config
        assert "mode" not in config.split("\n")[1]

    def test_applies_rotation(self):
        config = build_sway_config(make(), None, 270, True, "cmd")
        assert "transform 270" in config

    def test_hides_the_cursor_only_when_asked(self):
        assert "hide_cursor" in build_sway_config(make(), None, 0, True, "cmd")
        assert "hide_cursor" not in build_sway_config(make(), None, 0, False, "cmd")

    def test_disables_xwayland_and_sets_a_black_background(self):
        """Both are about what a support engineer sees: no Xwayland failure in
        the log, and black rather than undefined pixels around the page."""
        config = build_sway_config(make(), None, 0, True, "cmd")
        assert "xwayland disable" in config
        assert "output HDMI-A-1 bg #000000 solid_color" in config

    def test_launches_the_browser(self):
        config = build_sway_config(make(), None, 0, True, "hmi-browser https://x/ --zoom 0.8")
        assert "exec_always hmi-browser https://x/ --zoom 0.8" in config


class TestSessionEnvironment:
    def test_software_rendering_when_the_gpu_is_unusable(self):
        env = session_environment(make(accelerated=False))
        assert env["WLR_RENDERER"] == "pixman"
        assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"

    def test_leaves_the_renderer_alone_when_the_gpu_works(self):
        env = session_environment(make(accelerated=True))
        assert "WLR_RENDERER" not in env

    def test_forcing_pixman_overrides_detection(self):
        env = session_environment(make(accelerated=True), force_renderer="pixman")
        assert env["WLR_RENDERER"] == "pixman"

    def test_forcing_gl_overrides_detection(self):
        env = session_environment(make(accelerated=False), force_renderer="gl")
        assert "WLR_RENDERER" not in env

    def test_points_the_compositor_at_the_right_card(self):
        assert session_environment(make())["WLR_DRM_DEVICES"] == "/dev/dri/card1"


class TestConflictingServiceNames:
    """`config.Array` hands back ConfigElement children, not plain strings.

    Assuming otherwise crashed the app on its first real deployment — the unit
    tests had been passing strings straight in, so they never saw it.
    """

    @staticmethod
    def _names(raw):
        # The unwrapping logic from HMIEngineApplication, exercised without
        # needing a live pydoover config object.
        names = []
        for item in raw or []:
            name = getattr(item, "value", item)
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        return names

    class FakeElement:
        def __init__(self, value):
            self.value = value

    def test_unwraps_config_elements(self):
        raw = [self.FakeElement("S01splash"), self.FakeElement("S89splash")]
        assert self._names(raw) == ["S01splash", "S89splash"]

    def test_still_accepts_plain_strings(self):
        assert self._names(["S01splash"]) == ["S01splash"]

    def test_drops_blanks_and_whitespace(self):
        raw = [self.FakeElement(" S01splash "), self.FakeElement(""), self.FakeElement(None)]
        assert self._names(raw) == ["S01splash"]

    def test_handles_an_unset_array(self):
        assert self._names(None) == []


class TestBrowserSignalling:
    """Finding the browser to reload it.

    The compositor starts the browser, so this app never holds a handle to it —
    /proc is the only way back to the process, and getting it wrong means a
    redeployed widget silently keeps showing the old bundle.
    """

    def fake_proc(self, tmp_path, **pids):
        for pid, cmdline in pids.items():
            entry = tmp_path / str(pid)
            entry.mkdir()
            (entry / "cmdline").write_bytes(cmdline.encode())
        (tmp_path / "self").mkdir()  # /proc has non-numeric entries too
        return tmp_path

    def test_finds_the_browser_among_everything_else(self, tmp_path):
        proc = self.fake_proc(
            tmp_path,
            **{
                "1": "/usr/bin/python3\x00-m\x00hmi_engine\x00",
                "42": "sway\x00-c\x00/tmp/hmi-runtime/sway.conf\x00",
                "77": "/usr/bin/python3\x00/usr/local/lib/hmi_browser.py\x00https://localhost:49100/\x00",
            },
        )
        assert browser_pids(proc=proc) == [77]

    def test_finds_nothing_when_the_browser_is_not_running(self, tmp_path):
        proc = self.fake_proc(tmp_path, **{"42": "sway\x00"})
        assert browser_pids(proc=proc) == []

    def test_tolerates_a_process_exiting_mid_scan(self, tmp_path):
        proc = self.fake_proc(tmp_path, **{"9": "hmi_browser.py\x00"})
        (proc / "9" / "cmdline").unlink()
        assert browser_pids(proc=proc) == []

    def test_says_nothing_was_reloaded_when_there_is_no_browser(self, tmp_path):
        """The caller restarts the session on a zero, so this must not lie."""
        assert reload_page(proc=self.fake_proc(tmp_path)) == 0

    def test_signals_every_browser_it_finds(self, tmp_path, monkeypatch):
        proc = self.fake_proc(tmp_path, **{"77": "hmi_browser.py\x00"})
        sent = []
        monkeypatch.setattr(session_mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
        assert reload_page(proc=proc) == 1
        assert sent == [(77, signal.SIGHUP)]

    def test_a_process_that_dies_before_the_signal_is_not_counted(self, tmp_path, monkeypatch):
        proc = self.fake_proc(tmp_path, **{"77": "hmi_browser.py\x00"})

        def gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(session_mod.os, "kill", gone)
        assert reload_page(proc=proc) == 0
