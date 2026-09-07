"""The page itself: a fullscreen WebKit view and nothing else.

Runs as its own process, launched by the compositor. Kept deliberately small —
a kiosk browser's whole job is to show one URL, survive the page failing, and
get out of the way.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")

from gi.repository import GLib, Gtk, WebKit  # noqa: E402  (must follow require_version)

log = logging.getLogger(__name__)

#: How long to wait before retrying a page that failed to load. The device may
#: be showing a dashboard served by another container on the same box, which
#: can easily still be starting up.
RETRY_SECONDS = 5


class KioskWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, url: str, zoom: float, ignore_tls: bool):
        super().__init__(application=app)
        self.url = url
        self._retry_source: int | None = None

        self.set_decorated(False)
        self.fullscreen()

        settings = WebKit.Settings(
            enable_developer_extras=False,
            enable_write_console_messages_to_stdout=True,
            # A wall display has no user to hand a password to, and no keyboard
            # to type one with.
            enable_html5_database=True,
            enable_html5_local_storage=True,
            media_playback_requires_user_gesture=False,
        )

        self.view = WebKit.WebView(settings=settings)
        self.view.set_zoom_level(zoom)
        self.view.connect("load-failed", self._on_load_failed)
        self.view.connect("load-changed", self._on_load_changed)

        if ignore_tls:
            # Device-local pages are served over HTTPS with a self-signed
            # certificate; there is no CA to trust and no user to click through.
            self.view.get_network_session().set_tls_errors_policy(
                WebKit.TLSErrorsPolicy.IGNORE
            )

        self.set_child(self.view)
        self.load()

    def load(self) -> None:
        log.info("Loading %s", self.url)
        self.view.load_uri(self.url)

    def _on_load_changed(self, _view, event) -> None:
        if event == WebKit.LoadEvent.FINISHED:
            log.info("Loaded %s", self.url)
            # Report readiness on stdout so the supervisor can tell "showing the
            # page" from "showing an error" without scraping pixels.
            print("HMI-STATUS loaded", flush=True)

    def _on_load_failed(self, _view, _event, failing_uri, error) -> bool:
        log.warning("Load failed for %s: %s", failing_uri, error.message)
        print(f"HMI-STATUS failed {error.message}", flush=True)
        self._schedule_retry()
        return True  # we handled it; don't show WebKit's own error page

    def _schedule_retry(self) -> None:
        if self._retry_source is not None:
            return

        def retry() -> bool:
            self._retry_source = None
            self.load()
            return GLib.SOURCE_REMOVE

        self._retry_source = GLib.timeout_add_seconds(RETRY_SECONDS, retry)

    def reload_now(self) -> None:
        """Re-fetch the page and everything under it.

        Bypassing the cache is the point: the usual reason to be asked is that
        the app serving this page has just been redeployed underneath it, so a
        revalidating reload could put the old bundle straight back up.
        """
        log.info("Reloading %s", self.url)
        self.view.reload_bypass_cache()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Fullscreen WebKit kiosk window")
    parser.add_argument("url")
    parser.add_argument("--zoom", type=float, default=1.0)
    parser.add_argument("--ignore-tls", action="store_true")
    parser.add_argument(
        "--reload-minutes",
        type=float,
        default=0.0,
        help="Reload periodically; 0 disables. Guards against a page that has "
        "quietly wedged after days on screen.",
    )
    args = parser.parse_args(argv)

    app = Gtk.Application(application_id="com.doover.hmi")
    window: dict[str, KioskWindow] = {}

    def on_activate(application: Gtk.Application) -> None:
        win = KioskWindow(application, args.url, args.zoom, args.ignore_tls)
        window["win"] = win
        win.present()

        # The supervising app has no handle on this process — the compositor
        # started it — so SIGHUP is how a redeployed widget gets onto the panel
        # without blanking it by restarting the whole session.
        GLib.unix_signal_add(
            GLib.PRIORITY_DEFAULT,
            signal.SIGHUP,
            lambda: (win.reload_now(), GLib.SOURCE_CONTINUE)[1],
        )

        if args.reload_minutes > 0:
            GLib.timeout_add_seconds(
                int(args.reload_minutes * 60),
                lambda: (win.reload_now(), GLib.SOURCE_CONTINUE)[1],
            )

    app.connect("activate", on_activate)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
