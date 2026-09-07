"""Finding the app that wanted a screen, and turning it into a URL.

These are the paths that decide whether a wall-mounted panel shows the right
page or nothing at all, on a device nobody has a keyboard for.
"""

import pytest

from hmi_engine.source import (
    DEVICE_AGENT_URL,
    DEFAULT_URL,
    UnresolvedURL,
    agent_id_of,
    choose_source,
    find_widget_apps,
    resolve_url,
)


def aggregate(**apps):
    return {"applications": apps}


def install(widget=False, agent="7788", org="1234", application="some_app", channel="some_app_1_widget", **extra):
    entry = {
        "AGENT_ID": agent,
        "ORGANISATION_ID": org,
        "APPLICATION_NAME": application,
        **extra,
    }
    if widget:
        # The platform sets this to the widget's channel name, which is also
        # the route the device agent serves it from.
        entry["dv_widget_url"] = channel
    return entry


class TestFindWidgetApps:
    def test_finds_the_app_that_ships_a_widget(self):
        data = aggregate(
            hmi_engine_1=install(),
            platform=install(),
            indratel_demo_1=install(
                widget=True, application="indratel_demo", channel="indratel_demo_1_widget"
            ),
        )
        found = find_widget_apps(data, exclude="hmi_engine_1")
        assert [a.app_key for a in found] == ["indratel_demo_1"]
        assert found[0].application == "indratel_demo"
        assert found[0].agent_id == "7788"
        assert found[0].widget_channel == "indratel_demo_1_widget"

    def test_ignores_apps_without_a_widget(self):
        """`dv_widget_url` is set by the platform only for widget apps, so its
        absence is the signal — there is nothing for a widget repo to declare."""
        assert find_widget_apps(aggregate(analog_flow_meter_1=install())) == []

    def test_never_returns_itself(self):
        data = aggregate(hmi_engine_1=install(widget=True))
        assert find_widget_apps(data, exclude="hmi_engine_1") == []

    def test_survives_an_aggregate_that_has_not_arrived(self):
        assert find_widget_apps({}) == []
        assert find_widget_apps({"applications": None}) == []

    def test_is_stable_across_restarts(self):
        data = aggregate(
            zulu_1=install(widget=True), alpha_1=install(widget=True)
        )
        assert [a.app_key for a in find_widget_apps(data)] == ["alpha_1", "zulu_1"]


class TestAgentId:
    def test_prefers_our_own_entry(self):
        data = aggregate(
            hmi_engine_1=install(agent="111"), other_1=install(agent="222")
        )
        assert agent_id_of(data, "hmi_engine_1") == "111"

    def test_falls_back_to_any_entry(self):
        """Our own entry is written by our own deployment; borrowing another
        app's is better than refusing to start over a missing key."""
        assert agent_id_of(aggregate(other_1=install(agent="222")), "hmi_engine_1") == "222"

    def test_reports_nothing_when_the_device_is_silent(self):
        assert agent_id_of({}, "hmi_engine_1") == ""


class TestChooseSource:
    def test_uses_the_only_widget_app(self):
        found = find_widget_apps(aggregate(indratel_demo_1=install(widget=True)))
        assert choose_source(found, DEFAULT_URL).app_key == "indratel_demo_1"

    def test_no_widget_app_is_not_an_error(self):
        assert choose_source([], DEFAULT_URL) is None

    def test_two_widget_apps_need_picking(self):
        """The default URL names the app, so guessing would put the wrong
        dashboard on a wall. Ask instead."""
        found = find_widget_apps(aggregate(a_1=install(widget=True), b_1=install(widget=True)))
        with pytest.raises(UnresolvedURL, match="a_1, b_1"):
            choose_source(found, DEFAULT_URL)

    def test_two_widget_apps_are_fine_when_the_url_names_neither(self):
        found = find_widget_apps(aggregate(a_1=install(widget=True), b_1=install(widget=True)))
        assert choose_source(found, "http://localhost:8080").app_key == "a_1"

    def test_the_fix_for_ambiguity_is_writing_the_url_out(self):
        """There is no knob for picking one — the URL is the knob."""
        found = find_widget_apps(aggregate(a_1=install(widget=True), b_1=install(widget=True)))
        with pytest.raises(UnresolvedURL, match="set URL"):
            choose_source(found, DEFAULT_URL)


class TestResolveURL:
    def widget(self, key="indratel_demo_1", **kwargs):
        return find_widget_apps(
            aggregate(**{key: install(widget=True, channel=f"{key}_widget", **kwargs)})
        )[0]

    def test_builds_the_local_widget_page_by_default(self):
        """Served by the device agent on the device itself, which is why a
        panel with no keyboard never meets a login screen."""
        url = resolve_url(DEFAULT_URL, agent_id="7788", source=self.widget())
        assert url == (
            "https://localhost:49100/widget/indratel_demo_1_widget?app_key=indratel_demo_1"
        )

    def test_the_agent_url_is_not_configurable(self):
        """One knob. A device whose agent web port has moved writes the whole
        URL out instead."""
        url = resolve_url(DEFAULT_URL, agent_id="", source=self.widget())
        assert url.startswith(DEVICE_AGENT_URL + "/widget/")

    def test_a_plain_url_needs_nothing_from_the_device(self):
        """A device-local page must work on a device that has never deployed
        anything else — no aggregate, no agent id, no widget."""
        url = resolve_url("http://localhost:8080", agent_id="", source=None)
        assert url == "http://localhost:8080"

    def test_expands_every_placeholder(self):
        url = resolve_url(
            "{device_agent_url}/widget/{widget_channel}?app_key={app_key}&agent={agent_id}&org={org_id}",
            agent_id="7788",
            source=self.widget(agent="7788", org="1234"),
        )
        assert url == (
            "https://localhost:49100/widget/indratel_demo_1_widget"
            "?app_key=indratel_demo_1&agent=7788&org=1234"
        )

    def test_borrows_the_agent_id_from_the_source_app(self):
        url = resolve_url("https://x/{agent_id}", agent_id="", source=self.widget(agent="999"))
        assert url == "https://x/999"

    def test_says_what_is_missing_rather_than_showing_a_broken_page(self):
        with pytest.raises(UnresolvedURL, match="app_key, widget_channel"):
            resolve_url(DEFAULT_URL, agent_id="7788", source=None)

    def test_rejects_a_placeholder_it_cannot_fill(self):
        with pytest.raises(UnresolvedURL, match="nonsense"):
            resolve_url("https://x/{nonsense}", agent_id="7788", source=None)
