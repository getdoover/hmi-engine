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
