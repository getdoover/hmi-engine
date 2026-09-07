"""Work out what this device wants on its panel, from the config it already has.

A widget app puts itself on the panel by naming `hmi_engine` in its
`depends_on`. The platform then creates an install of this app alongside it — but with nothing in its
config, because a dependent install is created bare. Rather than ask the widget
app to fill that in, this app reads the device's `deployment_config` aggregate
and finds the app that pulled it in.

The aggregate holds every install on the device under `applications`, keyed by
install name, and the platform stamps `dv_widget_url` into an entry only when
that application ships a widget. That flag is the whole detection rule: no
declaration, no convention, nothing to keep in sync in the widget's repo.

The page itself is served by the device agent, not the cloud — `dv_widget_url`
is also the name of the channel the DDA serves the bundle from. A widget on the
panel is a local page, so there is no login for a device with no keyboard to
get past.
"""

from dataclasses import dataclass
from string import Formatter

#: Set by the platform on any install whose application ships a widget. Its
#: value is the widget's channel name, which is also its route on the DDA.
WIDGET_MARKER = "dv_widget_url"

#: The device agent's own web server, on its fixed default port. HTTPS with a
#: self-signed certificate, which is what `ignore_tls_errors` defaults to true
#: for. Not configurable: an agent whose web port has been moved is a device
#: nobody has, and a URL typed out in full covers it if one ever appears.
DEVICE_AGENT_URL = "https://localhost:49100"

DEFAULT_URL = "{device_agent_url}/widget/{widget_channel}?app_key={app_key}"


@dataclass(frozen=True)
class SourceApp:
    """An installed app with a widget — a candidate for the panel."""

    app_key: str
    agent_id: str
    org_id: str
    application: str
    display_name: str
    #: Channel the DDA serves this widget's bundle from, e.g. `foo_1_widget`.
    widget_channel: str


def find_widget_apps(aggregate: dict, exclude: str = "") -> list[SourceApp]:
    """Widget apps on this device, in install-name order.

    Order matters only for making the choice repeatable across restarts; when
    it actually decides anything the caller asks for a pinned `source_app`.
    """
    apps = (aggregate or {}).get("applications") or {}
    found = []
    for key, entry in sorted(apps.items()):
        if key == exclude or not isinstance(entry, dict):
            continue
        if not entry.get(WIDGET_MARKER):
            continue
        found.append(
            SourceApp(
                app_key=key,
                agent_id=str(entry.get("AGENT_ID") or ""),
                org_id=str(entry.get("ORGANISATION_ID") or ""),
                application=str(entry.get("APPLICATION_NAME") or ""),
                display_name=str(entry.get("APP_DISPLAY_NAME") or key),
                widget_channel=str(entry[WIDGET_MARKER]),
            )
        )
    return found


def agent_id_of(aggregate: dict, app_key: str) -> str:
    """This install's own agent id, for building a URL with no widget in sight.

    Every entry carries it, so any will do — but prefer our own, which is the
    one entry guaranteed to exist by the time the app is running.
    """
    apps = (aggregate or {}).get("applications") or {}
    entry = apps.get(app_key) or {}
    if entry.get("AGENT_ID"):
        return str(entry["AGENT_ID"])
    for other in apps.values():
        if isinstance(other, dict) and other.get("AGENT_ID"):
            return str(other["AGENT_ID"])
    return ""


def fields_in(template: str) -> set[str]:
    """Placeholder names in a URL template, ignoring literal text."""
    return {name for _, name, _, _ in Formatter().parse(template) if name}


class UnresolvedURL(Exception):
    """The URL needs something the device hasn't told us yet.

    Raised rather than returned because every caller has the same recourse:
    say so on a tag and try again later. The watchdog restarts the session
    every cycle, so an install created before its widget app finishes deploying
    fixes itself once the widget publishes its config.
    """


def resolve_url(
    template: str,
    *,
    agent_id: str,
    source: SourceApp | None,
) -> str:
    """Expand a URL template against what we know about this device.

    Templates keep one config profile usable across a fleet: every device
    resolves `{agent_id}` to its own. Anything the template doesn't ask for
    doesn't have to be resolvable — a plain `http://localhost:8080` never
    touches the aggregate at all.
    """
    wanted = fields_in(template)

    values = {"device_agent_url": DEVICE_AGENT_URL}
    if agent_id:
        values["agent_id"] = agent_id
    if source is not None:
        values.update(
            app_key=source.app_key,
            org_id=source.org_id,
            application=source.application,
            widget_channel=source.widget_channel,
        )
        # A source app's agent id is the same device, but it is present even
        # when our own entry somehow isn't.
        values.setdefault("agent_id", source.agent_id)

    missing = sorted(name for name in wanted if not values.get(name))
    if missing:
        raise UnresolvedURL(f"URL needs {', '.join(missing)}, which is not known yet")

    unknown = sorted(wanted - values.keys())
    if unknown:
        raise UnresolvedURL(f"URL has unknown placeholder(s): {', '.join(unknown)}")

    return template.format(**values)


def choose_source(candidates: list[SourceApp], template: str) -> SourceApp | None:
    """Pick the app to show, or explain why we can't.

    Ambiguity only matters when the template actually names the app, which the
    default one does — two widget apps on a device with a single panel is a
    question only a person can answer, and guessing puts the wrong dashboard on
    a wall. The answer is to write the URL out; there is no separate knob for
    picking one, because a device with two panels' worth of dashboards and one
    panel is already past what a default can decide.
    """
    if not candidates:
        return None

    if len(candidates) > 1 and "app_key" in fields_in(template):
        names = ", ".join(c.app_key for c in candidates)
        raise UnresolvedURL(
            f"Several widget apps here ({names}); set URL to the one you want"
        )

    return candidates[0]
