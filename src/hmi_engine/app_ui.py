from pathlib import Path

from pydoover import ui

from .app_tags import HMIEngineTags as Tags


class HMIEngineUI(ui.UI):
    showing = ui.BooleanVariable("Showing", value=Tags.showing, name="showing")
    output = ui.TextVariable("Output", value=Tags.output, name="output")
    mode = ui.TextVariable("Mode", value=Tags.mode, name="mode")
    renderer = ui.TextVariable("Renderer", value=Tags.renderer, name="renderer")
    url = ui.TextVariable("URL", value=Tags.url, name="url")

    restart = ui.Button("Restart Display", name="restart", position=1)

    diagnostics = ui.Submodule(
        "Diagnostics",
        name="diagnostics",
        children=[
            ui.BooleanVariable("Display Detected", value=Tags.display_found, name="display_found"),
            ui.NumericVariable("Restarts", value=Tags.restarts, name="restarts", precision=0),
            ui.TextVariable("Source App", value=Tags.source_app, name="source_app"),
            ui.TextVariable("Last Error", value=Tags.last_error, name="last_error"),
        ],
    )


def export():
    HMIEngineUI(None, None, None).export(
        Path(__file__).parents[2] / "doover_config.json",
        "hmi_engine",
    )


if __name__ == "__main__":
    export()
