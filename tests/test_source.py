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
    is_install_name,
    pick_named,
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

    def test_the_message_says_what_to_type(self):
        """A panel showed nothing for an afternoon because the old message read
        as "type a URL" and the operator typed an app name. It now asks for the
        name, and an example of one that would work."""
        found = find_widget_apps(aggregate(a_1=install(widget=True), b_1=install(widget=True)))
        with pytest.raises(UnresolvedURL, match="install name of the one you want, e.g. a_1"):
            choose_source(found, DEFAULT_URL)

    def test_the_instruction_survives_the_tag(self):
        """`last_error` is cut to 200 characters, and a device with a handful of
        widget apps writes more than that. Whatever is lost, it is names off the
        end of the list — never the sentence saying where to put one."""
        found = find_widget_apps(
            aggregate(
                **{
                    f"petronash_pump_controller_{n}": install(widget=True)
                    for n in range(1, 8)
                }
            )
        )
        with pytest.raises(UnresolvedURL) as caught:
            choose_source(found, DEFAULT_URL)
        assert len(str(caught.value)) > 200
        assert "set URL to the install name" in str(caught.value)[:200]
        assert "e.g. petronash_pump_controller_1" in str(caught.value)[:200]


class TestIsInstallName:
    def test_a_bare_name_is_a_name(self):
        assert is_install_name("petronash_hmi_1")

    def test_blank_is_not(self):
        """Blank means "work it out", which is the normal case."""
        assert not is_install_name("")
        assert not is_install_name("   ")

    def test_a_url_is_not(self):
        assert not is_install_name("http://localhost:8080")
        assert not is_install_name("https://x.doover.com/agent/1")

    def test_a_template_is_not(self):
        assert not is_install_name(DEFAULT_URL)
        assert not is_install_name("{device_agent_url}/widget/x")

    def test_anything_with_a_space_is_not(self):
        """Not a name we could match, and not a URL either — treat it as a URL
        so the browser reports it rather than us guessing at an app."""
        assert not is_install_name("petronash hmi 1")


class TestPickNamed:
    def device(self):
        return aggregate(
            data_report_segmenter_1=install(
                widget=True, application="data_report_segmenter"
            ),
            petronash_hmi_1=install(widget=True, application="petronash_hmi"),
            petronash_pump_controller_1=install(
                widget=True, application="petronash_pump_controller"
            ),
            modbus_bridge_1=install(application="modbus_bridge"),
        )

    def candidates(self, data=None):
        return find_widget_apps(data or self.device(), exclude="hmi_engine_1")

    def test_matches_the_install_name(self):
        """What the ambiguity message asks for, and what was typed on the day
        this was needed."""
        picked = pick_named(self.candidates(), "petronash_hmi_1", self.device())
        assert picked.app_key == "petronash_hmi_1"

    def test_matches_a_unique_application_name(self):
        """The install suffix is platform bookkeeping; nobody should have to
        know it when only one install could be meant."""
        picked = pick_named(self.candidates(), "petronash_hmi", self.device())
        assert picked.app_key == "petronash_hmi_1"

    def test_two_installs_of_one_application_still_need_picking(self):
        data = aggregate(
            pump_1=install(widget=True, application="pump"),
            pump_2=install(widget=True, application="pump"),
        )
        with pytest.raises(UnresolvedURL, match="pump_1, pump_2"):
            pick_named(self.candidates(data), "pump", data)

    def test_an_app_that_is_here_but_ships_no_widget_says_so(self):
        """Different mistake, different answer: the name was right, the app
        just has no page to show."""
        with pytest.raises(UnresolvedURL, match="ships no widget"):
            pick_named(self.candidates(), "modbus_bridge_1", self.device())

    def test_by_application_name_too(self):
        """The console shows applications, so `modbus_bridge` is at least as
        likely to be typed as the install key — and it is the same mistake."""
        with pytest.raises(UnresolvedURL, match="ships no widget"):
            pick_named(self.candidates(), "modbus_bridge", self.device())

    def test_an_unknown_name_lists_what_is_here(self):
        with pytest.raises(UnresolvedURL, match="petronash_hmi_1, petronash_pump_controller_1"):
            pick_named(self.candidates(), "petronash_hmy_1", self.device())

    def test_says_when_nothing_here_has_a_widget(self):
        """An install deployed before its widget app — listing an empty set of
        candidates would read as "your name is wrong"."""
        data = aggregate(modbus_bridge_1=install())
        with pytest.raises(UnresolvedURL, match="ships a widget yet"):
            pick_named(self.candidates(data), "petronash_hmi_1", data)

    def test_matching_is_exact(self):
        with pytest.raises(UnresolvedURL, match="No widget app called"):
            pick_named(self.candidates(), "Petronash_HMI_1", self.device())


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

    def test_a_half_typed_template_is_a_config_mistake_not_a_crash(self):
        """A missing brace used to raise ValueError out of `start_session`,
        which takes the app down; every other thing wrong with this box puts a
        line on `last_error` and waits for the watchdog."""
        with pytest.raises(UnresolvedURL, match="not a usable template"):
            resolve_url("https://x/{widget_channel", agent_id="7788", source=None)

    def test_a_bad_format_spec_is_the_same_mistake(self):
        with pytest.raises(UnresolvedURL, match="not a usable template"):
            resolve_url("https://x/{agent_id:d}", agent_id="7788", source=None)
