"""HMI engine app.

`main` imports the application lazily so that `display` and `session` stay
importable on their own — they have no pydoover dependency, and the browser
process runs on a different interpreter that does not have one.
"""


def main():
    """Run the application."""
    from pydoover.docker import run_app

    from .application import HMIEngineApplication

    run_app(HMIEngineApplication())
