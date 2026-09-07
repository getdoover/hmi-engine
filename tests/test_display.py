"""Tests for the display detection, which is the part that has to cope with
hardware nobody has tried yet."""

import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from hmi_engine.display import Display, Mode, find_host_compositor, resolve_mode


def make(modes, connector="HDMI-A-1"):
    return Display(
        connector=connector,
        device="/dev/dri/card1",
        modes=tuple(modes),
        accelerated=False,
        dri_path=None,
    )


class TestMode:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1280x720", Mode(1280, 720)),
            ("1280x720@60", Mode(1280, 720, 60)),
            ("1280x720@60Hz", Mode(1280, 720, 60)),
            (" 1920 X 1080 ", Mode(1920, 1080)),
        ],
    )
    def test_parses_the_shapes_people_actually_type(self, text, expected):
        assert Mode.parse(text) == expected

    @pytest.mark.parametrize("text", ["", "720p", "1280*720", "x720", None])
    def test_rejects_nonsense(self, text):
        assert Mode.parse(text) is None

    def test_renders_back_to_the_form_sway_wants(self):
        assert str(Mode(1280, 720, 60)) == "1280x720@60Hz"
        assert str(Mode(1280, 720)) == "1280x720"


class TestResolveMode:
    def test_prefers_the_connectors_first_mode(self):
        display = make([Mode(1920, 1080), Mode(1280, 720)])
        assert resolve_mode(display, "") == Mode(1920, 1080)

    def test_honours_a_supported_request(self):
        display = make([Mode(1920, 1080), Mode(1280, 720)])
        assert resolve_mode(display, "1280x720@60") == Mode(1280, 720, 60)

    def test_tries_an_unadvertised_mode_anyway(self):
        """Panels routinely accept modes they do not list, and refusing would
        turn a working setup into a black screen."""
        display = make([Mode(1920, 1080)])
        assert resolve_mode(display, "1280x720") == Mode(1280, 720)

    def test_falls_back_when_the_request_is_unparseable(self):
        display = make([Mode(1920, 1080)])
        assert resolve_mode(display, "very large") == Mode(1920, 1080)

    def test_survives_a_connector_that_lists_no_modes(self):
        assert resolve_mode(make([]), "") is None


@pytest.fixture
def run_user():
    """A stand-in for /run/user.

    Under /tmp rather than pytest's tmp_path because an AF_UNIX path is capped
    at around a hundred bytes and the usual fixture path spends most of that
    before the socket is named.
    """
    root = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def session(run_user, uid, listening=True, name="wayland-0"):
    """Create a compositor socket for `uid`, live or abandoned."""
    directory = run_user / str(uid)
    directory.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(directory / name))
    if listening:
        # An abandoned socket is bound but unlistened: the file is there and
        # connecting to it is refused, which is exactly the stale case.
        sock.listen(1)
    return sock


class TestFindHostCompositor:
    def test_finds_a_live_session(self, run_user):
        sock = session(run_user, 1000)
        try:
            found = find_host_compositor(run_user)
            assert found is not None
            assert found.uid == 1000
            assert found.socket_path == run_user / "1000" / "wayland-0"
        finally:
            sock.close()

    def test_reports_the_socket_as_an_absolute_path(self, run_user):
        # libwayland only skips XDG_RUNTIME_DIR when the value starts with "/".
        sock = session(run_user, 1000)
        try:
            assert find_host_compositor(run_user).wayland_display.startswith("/")
        finally:
            sock.close()

    def test_ignores_a_socket_nothing_is_listening_on(self, run_user):
        sock = session(run_user, 1000, listening=False)
        try:
            assert find_host_compositor(run_user) is None
        finally:
            sock.close()

    def test_ignores_the_lock_file_beside_the_socket(self, run_user):
        sock = session(run_user, 1000)
        (run_user / "1000" / "wayland-0.lock").write_text("")
        try:
            assert find_host_compositor(run_user).socket_path.name == "wayland-0"
        finally:
            sock.close()

    def test_prefers_a_logged_in_user_over_a_system_account(self, run_user):
        # A greeter running as root is not the session that owns the screen.
        root_sock = session(run_user, 0)
        user_sock = session(run_user, 1000)
        try:
            assert find_host_compositor(run_user).uid == 1000
        finally:
            root_sock.close()
            user_sock.close()

    def test_falls_back_to_a_system_account_when_that_is_all_there_is(self, run_user):
        sock = session(run_user, 0)
        try:
            assert find_host_compositor(run_user).uid == 0
        finally:
            sock.close()

    def test_no_compositor_when_no_session_has_a_socket(self, run_user):
        (run_user / "1000").mkdir()
        assert find_host_compositor(run_user) is None

    def test_no_compositor_on_a_device_without_run_user(self, run_user):
        # The boards this app was written for have no logind and no /run/user.
        assert find_host_compositor(run_user / "missing") is None
