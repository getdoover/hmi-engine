from pathlib import Path

from pydoover import config


class HMIEngineConfig(config.Schema):
    """Everything is optional, including the URL.

    Defaults are all "work it out": the app finds the connected display, its
    preferred mode, whether the GPU can be used, and — when no URL is given —
    which app on this device wanted a screen in the first place. A bare install
    on unfamiliar hardware still shows the right page.
    """

    url = config.String(
        "URL",
        default=None,
        description=(
            "The page to display. Leave blank to show the widget of the app "
            "that named hmi_engine in its dependencies, served locally by the "
            "device agent. If more than one app here has a widget, put the "
            "install name of the one you want (e.g. petronash_hmi_1). A full "
            "URL also works and may use {device_agent_url}, {widget_channel}, "
            "{app_key}, {agent_id} and {org_id}, so one config profile works "
            "across a fleet."
        ),
    )

    zoom = config.Number(
        "Zoom",
        default=1.0,
        minimum=0.25,
        maximum=4.0,
        description=(
            "Page zoom. Below 1 fits more on screen — useful for driving a "
            "dashboard designed for a desktop onto a 720p panel."
        ),
    )

    output = config.String(
        "Output",
        default=None,
        description=(
            "Connector to use, e.g. 'HDMI-A-1'. Leave blank to use the first "
            "connected output."
        ),
    )

    mode = config.String(
        "Mode",
        default=None,
        description=(
            "Display mode, e.g. '1280x720@60'. Leave blank for the panel's "
            "preferred mode. Driving a 1080p panel at 720p roughly halves the "
            "work when there is no GPU."
        ),
    )

    rotation = config.Enum(
        "Rotation",
        default="0",
        choices=["0", "90", "180", "270"],
        description="Screen rotation in degrees, for a panel mounted sideways.",
    )

    renderer = config.Enum(
        "Renderer",
        default="auto",
        choices=["auto", "gl", "pixman"],
        description=(
            "'auto' uses the GPU when Mesa can drive it and falls back to "
            "software. Force 'pixman' if the GPU is present but unusable."
        ),
    )

    reload_minutes = config.Number(
        "Reload Interval (min)",
        default=0.0,
        minimum=0.0,
        description=(
            "Reload the page on this interval. 0 never reloads. A guard against "
            "a page that has quietly wedged after weeks on a wall."
        ),
    )

    reload_at = config.String(
        "Reload At",
        default=None,
        description=(
            "Reload the page once a day at this time, 24-hour HH:MM (e.g. "
            "00:00). Leave blank for no daily reload. The same guard as the "
            "interval, but at an hour nobody is using the panel."
        ),
    )

    timezone = config.String(
        "Timezone",
        default="UTC",
        description=(
            "The timezone Reload At is in, as an IANA name such as "
            "Australia/Brisbane. Blank means UTC."
        ),
    )

    memory_limit_mb = config.Number(
        "Memory Limit (MB)",
        default=320.0,
        minimum=0.0,
        description=(
            "Private memory the page may use, in MB. Not RSS, which also "
            "counts ~100 MB of shared libraries. Past half of it the browser "
            "frees caches; at 1.25x it restarts the page rather than let the "
            "device run out and swap. The SIA HMI uses about 110 MB private "
            "(250 RSS). 0 leaves the browser's own default, which never "
            "restarts it."
        ),
    )

    hide_cursor = config.Boolean(
        "Hide Cursor",
        default=True,
        description="Hide the mouse pointer. There is rarely a mouse.",
    )

    ignore_tls_errors = config.Boolean(
        "Ignore TLS Errors",
        default=True,
        description=(
            "Accept self-signed certificates. Device-local pages are usually "
            "served with one and there is no user to click through the warning."
        ),
    )

    stop_services = config.Array(
        "Conflicting Services",
        element=config.String("Service"),
        description=(
            "Init scripts to stop before starting, for vendor images that run "
            "their own splash on the framebuffer and would repaint over this. "
            "On an ELPRO Quantum: S01splash and S89splash."
        ),
    )


def export():
    HMIEngineConfig.export(Path(__file__).parents[2] / "doover_config.json", "hmi_engine")


if __name__ == "__main__":
    export()
