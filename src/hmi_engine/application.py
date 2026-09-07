import asyncio
import logging
import os
import shlex

from pydoover import ui
from pydoover.docker import Application
from pydoover.models import EventSubscription

from .app_config import HMIEngineConfig
from .app_tags import HMIEngineTags
from .app_ui import HMIEngineUI
from . import display as display_mod
from .session import (
    Session,
    build_sway_config,
    reload_page,
    session_environment,
    stop_conflicting_services,
)
from .source import (
    DEFAULT_URL,
    UnresolvedURL,
    agent_id_of,
    choose_source,
    find_widget_apps,
    resolve_url,
)

log = logging.getLogger(__name__)


class HMIEngineApplication(Application):
    """Put a web page on the device's own display and keep it there.

    The app owns a compositor and a browser as child processes. Its main loop is
    a watchdog: if the session dies — a wedged browser, a panel unplugged and
    replugged, a vendor splash winning a fight for the framebuffer — it brings
    the whole thing back rather than leaving a black screen nobody notices.
    """

    config_cls = HMIEngineConfig
    tags_cls = HMIEngineTags
    ui_cls = HMIEngineUI

    config: HMIEngineConfig
    tags: HMIEngineTags

    #: The widget channel carries the bundle itself — an update there is the
    #: new JavaScript having landed, so there is nothing to wait for beyond
    #: collapsing a burst of writes into one reload.
    BUNDLE_DELAY = 1

    async def setup(self):
        self.session = Session()
        self._restarts = 0
        self._widget_channel = ""
        self._watched: set[str] = set()
        self._reload_task: asyncio.Task | None = None
        await self.start_session()

    async def start_session(self):
        """Work out what to show, detect the display, then bring up the session."""
        await self.tags.showing.set(False)

        try:
            url = await self.resolve_url()
        except UnresolvedURL as exc:
            # Usually an install that deployed before the app that pulled it in.
            # The watchdog comes back every cycle, so this resolves itself.
            await self.tags.last_error.set(str(exc)[:200])
            log.warning("Nothing to show yet: %s", exc)
            return

        stopped = await stop_conflicting_services(self._conflicting_service_names())
        if stopped:
            log.info("Stopped before starting: %s", ", ".join(stopped))

        found = display_mod.detect(self.config.output.value or "")
        await self.tags.display_found.set(found is not None)
        if found is None:
            await self.tags.last_error.set("No connected display found")
            log.error("No connected display; will retry")
            return

        mode = display_mod.resolve_mode(found, self.config.mode.value or "")
        renderer = self.config.renderer.value or "auto"
        effective = "gl" if (found.accelerated if renderer == "auto" else renderer == "gl") else "pixman"

        log.info(
            "Display %s on %s, mode %s, renderer %s (%s)",
            found.connector, found.device, mode, effective,
            "auto-detected" if renderer == "auto" else "forced",
        )

        config_text = build_sway_config(
            display=found,
            mode=mode,
            rotation=int(self.config.rotation.value or 0),
            hide_cursor=bool(self.config.hide_cursor.value),
            browser_command=self._browser_command(url),
        )

        try:
            await self.session.start(config_text, session_environment(found, renderer))
        except Exception as exc:  # noqa: BLE001 — surface any startup failure as a tag
            await self.tags.last_error.set(str(exc)[:200])
            log.exception("Could not start the display session")
            return

        await self.tags.output.set(found.connector)
        await self.tags.mode.set(str(mode) if mode else "preferred")
        await self.tags.renderer.set(effective)
        await self.tags.url.set(url)
        await self.tags.last_error.set("")
        await self.tags.showing.set(True)

    async def resolve_url(self) -> str:
        """The page to show, which the device usually already knows.

        A widget app that names `hmi_engine` in `depends_on` gets an
        install created for it automatically — and created bare, because the
        platform has nowhere to put config for a dependent. So a blank URL is
        not a misconfiguration, it is the normal case: find the app that ships
        a widget and show the copy the device agent serves locally.
        """
        configured = (self.config.url.value or "").strip()
        template = configured or DEFAULT_URL

        source = None
        agent_id = ""
        if "{" in template:
            aggregate = await self._deployment_config()
            source = choose_source(find_widget_apps(aggregate, self.app_key), template)
            agent_id = agent_id_of(aggregate, self.app_key)

            if not configured and source is None:
                raise UnresolvedURL("No app with a widget on this device yet")

        await self.tags.source_app.set(source.app_key if source else "")
        if source is not None:
            log.info("Showing %s (%s)", source.display_name, source.app_key)
            self._watch_widget_channel(source.widget_channel)

        return resolve_url(template, agent_id=agent_id, source=source)

    async def _deployment_config(self) -> dict:
        """Every install's config on this device, not just our own.

        Fetched once at startup rather than subscribed to: `self.config` only
        ever holds this app's own entry, and the rest of the device is what
        says which app wants the panel.

        A failed fetch is reported as an unresolved URL rather than allowed to
        propagate: the watchdog's answer to both is to try again shortly, and
        an exception out of `setup` takes the whole app down instead.
        """
        try:
            aggregate = await self.device_agent.fetch_channel_aggregate("deployment_config")
        except Exception as exc:  # noqa: BLE001 — any transport failure reads the same
            raise UnresolvedURL(f"Could not read this device's config: {exc}") from exc
        return getattr(aggregate, "data", None) or {}

    def _watch_widget_channel(self, channel: str) -> None:
        """Subscribe to the channel the widget's bundle is published on.

        A deployment of the widget app writes the new JavaScript here, which is
        a far more precise signal than the config aggregate: it fires when the
        page has actually changed, and not when an unrelated app on the same
        device is redeployed.

        Subscriptions can't be removed, so on the rare occasion the source app
        changes, the old callback stays registered and no-ops — it checks the
        channel it was woken for against the one currently on screen.
        """
        self._widget_channel = channel
        if channel in self._watched:
            return

        self.device_agent.add_event_callback(
            channel, self._on_bundle_changed, EventSubscription.aggregate_update
        )
        self._watched.add(channel)
        log.info("Watching %s for new widget builds", channel)

    async def _on_bundle_changed(self, event):
        """The widget's bundle was republished — put it on screen.

        This is the only thing the app reloads for. Config changes need no
        subscription: editing an install's config redeploys it, and redeploying
        this app restarts the container with the new config already in hand.
        """
        channel = getattr(getattr(event, "channel", None), "name", "")
        if channel != self._widget_channel:
            return  # a stale subscription from a previous source app

        if self._reload_task and not self._reload_task.done():
            self._reload_task.cancel()
        self._reload_task = asyncio.create_task(self._reload_after_delay())

    async def _reload_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.BUNDLE_DELAY)
            await self.reload_now()
        except asyncio.CancelledError:
            raise  # a later build superseded this one
        except Exception:  # noqa: BLE001 — a failed reload must not kill the app
            log.exception("Could not reload the display")

    async def reload_now(self) -> None:
        """Re-fetch the page in place, without blanking the panel."""
        if not self.session.running:
            return  # the watchdog owns this case

        if reload_page():
            log.info("Reloaded the page after a new widget build")
        else:
            # No browser to signal, but a live compositor — restarting is the
            # honest fallback rather than leaving a stale page on the wall.
            log.warning("No browser process to reload; restarting the session")
            await self.session.stop()
            await self.start_session()

    def _conflicting_service_names(self) -> list[str]:
        """Names out of the `Array` config element.

        An Array hands back its `ConfigElement` children, not plain strings —
        each one has to be unwrapped. Getting this wrong crashed the app on its
        first real deployment, because the tests had passed strings directly.
        """
        names = []
        for item in self.config.stop_services.value or []:
            name = getattr(item, "value", item)
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        return names

    #: The browser runs on the distro Python rather than the app's venv — see
    #: the Dockerfile. Overridable so it can be pointed at a dev checkout.
    BROWSER = os.environ.get(
        "HMI_BROWSER_COMMAND", "/usr/bin/python3 /usr/local/lib/hmi_browser.py"
    )

    def _browser_command(self, url: str) -> str:
        args = [*self.BROWSER.split(), url or "about:blank"]
        if self.config.zoom.value:
            args += ["--zoom", str(self.config.zoom.value)]
        if self.config.ignore_tls_errors.value:
            args.append("--ignore-tls")
        if self.config.reload_minutes.value:
            args += ["--reload-minutes", str(self.config.reload_minutes.value)]
        return shlex.join(args)

    async def main_loop(self):
        if self.session.running:
            await self.tags.showing.set(True)
            return

        # The session is down. Whether it never started or fell over, the fix is
        # the same, and doing it quietly beats a dark screen.
        self._restarts += 1
        await self.tags.restarts.set(self._restarts)
        await self.tags.showing.set(False)
        log.warning("Display session is not running; restarting (attempt %s)", self._restarts)

        await self.session.stop()
        await self.start_session()

    @ui.handler("restart")
    async def on_restart(self, ctx, value):
        log.info("Restart requested from the Doover UI")
        await self.session.stop()
        await self.start_session()

    async def on_shutdown(self):
        if self._reload_task and not self._reload_task.done():
            self._reload_task.cancel()
        await self.session.stop()
