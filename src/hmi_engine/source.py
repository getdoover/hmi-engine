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


class UnresolvedURL(Exception):
    """The URL needs something the device hasn't told us yet.

    Raised rather than returned because every caller has the same recourse:
    say so on a tag and try again later. The watchdog restarts the session
    every cycle, so an install created before its widget app finishes deploying
    fixes itself once the widget publishes its config.
    """


def fields_in(template: str) -> set[str]:
    """Placeholder names in a URL template, ignoring literal text.

    A half-typed template — `{widget_channel` with the brace missing — makes
    `Formatter.parse` raise `ValueError`, which is a typo in a config box, not
    a bug here. Letting it out takes the app down; as an `UnresolvedURL` it
    lands on `last_error` like every other thing wrong with this knob.
    """
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        raise UnresolvedURL(f"URL is not a usable template: {exc}") from None
    return {name for _, name, _, _ in parsed if name}


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

    try:
        return template.format(**values)
    except (ValueError, KeyError, IndexError) as exc:
        # `fields_in` has already vetted the names; what is left is a bad
        # conversion or format spec (`{agent_id:d}`), which is still a typo.
        raise UnresolvedURL(f"URL is not a usable template: {exc}") from None


def is_install_name(value: str) -> bool:
    """True when a configured `url` is really the name of an app on this device.

    When two widget apps share a panel this app asks the operator to pick one by
    name, so a bare name is exactly what gets typed back into the box. Anything
    written as a URL carries a scheme, a path or a placeholder, so a name never
    swallows a page someone meant to show — with one exception: a scheme-less
    address (`192.168.1.50`) looks exactly like a name, and is read as one.
    That costs nothing, because the browser rejects a scheme-less string too;
    `pick_named` just answers it with a message that names the http:// case.
    """
    value = (value or "").strip()
    if not value:
        return False
    return not any(c in value for c in "{}/:") and not any(c.isspace() for c in value)


def pick_named(candidates: list[SourceApp], name: str, aggregate: dict) -> SourceApp:
    """The widget app the operator named, or a message that helps them fix it.

    An install name is what the ambiguity message asks for, but people also type
    the application name — one install of it is still unambiguous. Everything
    else is a typo or a misunderstanding, and each has its own answer: naming an
    app that is here but ships no widget is a different mistake from naming one
    that isn't here at all, and a panel showing nothing is no help in telling
    them apart.

    Every message here leads with what to do, because `last_error` is truncated
    to 200 characters and a list of installs can run past that on its own.
    """
    for app in candidates:
        if app.app_key == name:
            return app

    same_application = [a for a in candidates if a.application == name]
    if len(same_application) == 1:
        return same_application[0]
    if same_application:
        keys = ", ".join(a.app_key for a in same_application)
        raise UnresolvedURL(
            f"Set URL to the install name of the {name} install you want, e.g. "
            f"{same_application[0].app_key}. They are: {keys}"
        )

    apps = (aggregate or {}).get("applications") or {}
    # An operator reads application names in the console, not install keys, so
    # `modbus_bridge` has to reach the same answer as `modbus_bridge_1`.
    if name in apps or any(
        isinstance(e, dict) and str(e.get("APPLICATION_NAME") or "") == name
        for e in apps.values()
    ):
        raise UnresolvedURL(
            f"{name} is installed here but ships no widget; "
            "set URL to a page to display instead"
        )

    if not candidates:
        raise UnresolvedURL(
            f"No widget app called {name} on this device, and nothing here "
            "ships a widget yet; a page of your own needs its http://"
        )
    here = ", ".join(c.app_key for c in candidates)
    raise UnresolvedURL(
        f"No widget app called {name} here; set URL to one of {here}, or to a "
        "page of your own with its http://"
    )


def choose_source(candidates: list[SourceApp], template: str) -> SourceApp | None:
    """Pick the app to show, or explain why we can't.

    Ambiguity only matters when the template actually names the app, which the
    default one does — two widget apps on a device with a single panel is a
    question only a person can answer, and guessing puts the wrong dashboard on
    a wall. The answer goes in the same box, so the message says what to type:
    an install name, which `pick_named` resolves. There is still no separate
    knob for picking one.

    The instruction comes before the list of installs because `last_error` is
    truncated to 200 characters: four installs are enough to run past that, and
    losing the list still leaves an operator told what to do, where losing the
    instruction leaves them with names and no idea where to put one.
    """
    if not candidates:
        return None

    if len(candidates) > 1 and "app_key" in fields_in(template):
        names = ", ".join(c.app_key for c in candidates)
        raise UnresolvedURL(
            "Several widget apps here; set URL to the install name of the one "
            f"you want, e.g. {candidates[0].app_key}. They are: {names}"
        )

    return candidates[0]
