from pydoover.tags import Tag, Tags


class HMIEngineTags(Tags):
    """What the app found and what it is doing, so a display that is not
    showing what it should can be diagnosed without a monitor or an SSH key."""

    display_found = Tag("boolean", default=False)
    showing = Tag("boolean", default=False)

    #: "own" when this app brought up sway, "host" when it attached to a
    #: compositor the device was already running. Which one is the first thing
    #: to know about a panel that is showing the wrong thing.
    compositor = Tag("string", default="")

    output = Tag("string", default="")
    mode = Tag("string", default="")
    renderer = Tag("string", default="")
    url = Tag("string", default="")

    #: The app whose page this is showing, when it was worked out rather
    #: than configured. Empty for a URL typed in by hand.
    source_app = Tag("string", default="")

    # Populated when something is wrong; empty when it isn't.
    last_error = Tag("string", default="")
    restarts = Tag("number", default=0)

    #: Times the page's web process has died (crashed, or killed by WebKit at
    #: `memory_limit_mb`) since the app started. Each is reloaded on its own;
    #: the count is how you tell a one-off from a page that keeps falling over.
    page_crashes = Tag("number", default=0)
    #: The latest of those: reason and UTC time, e.g.
    #: "exceeded-memory-limit at 2026-10-01 03:12:44 UTC". Kept after the page
    #: recovers, unlike `last_error`.
    last_page_crash = Tag("string", default="")
    #: Resident memory of the page's web process in MiB, updated once a minute.
    #: A steady climb over days is a leak on its way to the memory limit.
    page_memory_mb = Tag("number", default=0.0)
