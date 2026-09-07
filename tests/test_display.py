"""Tests for the display detection, which is the part that has to cope with
hardware nobody has tried yet."""

import pytest

from hmi_engine.display import Display, Mode, resolve_mode


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
