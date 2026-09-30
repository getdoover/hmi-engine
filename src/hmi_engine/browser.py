"""The page itself: a fullscreen WebKit view and nothing else.

Runs as its own process, launched by the compositor. Kept deliberately small —
a kiosk browser's whole job is to show one URL, survive the page failing, and
get out of the way.

"Survive the page failing" includes the page's web process dying under it.
WebKit renders in a separate WebKitWebProcess; if that crashes or is killed,
the view goes blank and stays blank unless something loads the page again. On
a panel that runs for weeks on a 1.8 GB Doovit, that is the difference between
a hiccup and a dark wall until someone walks up to it.

The memory limit (`--memory-limit-mb`) applies to the web process's *private*
footprint — about Private_Dirty in /proc/<pid>/smaps_rollup — not to its RSS.
On a Doovit RSS carries ~100 MB of shared libraries on top of that. Measured
with the SIA HMI: ~105-115 MB private, ~250-260 MB RSS. Pick a limit against
the private number; the once-a-minute memory log shows both.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")

from gi.repository import GLib, Gtk, WebKit  # noqa: E402  (must follow require_version)

log = logging.getLogger(__name__)

#: How long to wait before retrying a page that failed to load. The device may
#: be showing a dashboard served by another container on the same box, which
#: can easily still be starting up.
RETRY_SECONDS = 5

#: How often the web process's memory is read and reported. WebKit's own
#: memory-pressure check runs every `POLL_SECONDS`; reporting at twice that is
#: enough to draw a leak's slope and cheap enough to never matter.
MEMORY_REPORT_SECONDS = 60

# --- Memory pressure -------------------------------------------------------
#
# WHICH NUMBER THE LIMIT APPLIES TO: the web process's *private* footprint —
# roughly its Private_Dirty in /proc/<pid>/smaps_rollup — not RSS. RSS also
# counts ~100 MB of shared libraries on a Doovit, which WebKit ignores. Measured
# on the bench with the SIA HMI: ~105-115 MB private, ~250-260 MB RSS
# (Rss 262 = Shared_Clean 97 + Shared_Dirty 4 + Private_Clean 45 +
# Private_Dirty 114). A limit of 160 (kill at 200) never fired at 247 MB RSS; a
# limit of 64 (kill at 80) fired at once. So: private ≈ RSS minus ~100-150 MB.
#
# The device has 1.8 GB and about 690 MB available to the page. Past that it is
# in swap, and deep in swap the kernel's OOM killer picks its victim at random.
# These settings make WebKit shed memory as the page grows and, if that isn't
# enough, restart the page itself before the kernel has to choose.
#
# All thresholds are fractions of the limit (`--memory-limit-mb`, 320 by
# default), and all the MB below are private footprint.

#: 0.5 x 320 = 160 MB: start releasing non-critical memory (caches). WebKit's
#: default is 0.33, which suits its default limit (the machine's RAM, up to
#: 3 GB); against 320 it is 106 MB, right at the page's steady state, and the
#: handler would be trimming caches on every poll forever — the performance
#: trap WebKit's docs warn about.
CONSERVATIVE_THRESHOLD = 0.5

#: 0.75 x 320 = 240 MB: release critical memory too. Over twice the page's
#: normal size, so reaching it means the page is leaking, not just busy.
STRICT_THRESHOLD = 0.75

#: 1.25 x 320 = 400 MB: WebKit kills the web process, `web-process-terminated`
#: fires with EXCEEDED_MEMORY_LIMIT, and the page is reloaded fresh. 400 MB
#: private is roughly 500 MB RSS, inside the ~690 MB the device can give before
#: swapping, with room left for everything else on the box. The kill threshold
#: may exceed 1; 0 would mean "never kill", which is WebKit's default and the
#: reason a leak used to end in swap.
KILL_THRESHOLD = 1.25

#: WebKit's default, stated so it isn't a mystery: a poll every 30 s is plenty
#: for a leak that takes days, and a faster one costs CPU on a software-rendered
#: panel.
POLL_SECONDS = 30.0


def memory_pressure_settings(limit_mb: int) -> WebKit.MemoryPressureSettings | None:
    """WebKit memory-pressure settings for `limit_mb`, or None for WebKit's default."""
    if limit_mb <= 0:
        return None
    settings = WebKit.MemoryPressureSettings.new()
    settings.set_memory_limit(int(limit_mb))
    # Strict before conservative before kill. Each must sit between its
    # neighbours (conservative < strict < kill, unless kill is 0), and the
    # defaults are 0.33 / 0.5 / 0: setting conservative to 0.5 first would
    # momentarily equal the default strict and be rejected.
    settings.set_strict_threshold(STRICT_THRESHOLD)
    settings.set_conservative_threshold(CONSERVATIVE_THRESHOLD)
    settings.set_kill_threshold(KILL_THRESHOLD)
    settings.set_poll_interval(POLL_SECONDS)
    return settings


def limited_web_context(limit_mb: int) -> WebKit.WebContext | None:
    """Apply the memory limit to both WebKit child processes.

    Returns the web context to build the view with, or None to use WebKit's
    default one (no limit configured).

    Two separate mechanisms, and the order matters:

    - The network process takes its settings from a *class-level* call,
      `NetworkSession.set_memory_pressure_settings`, which only affects network
      sessions created after it. A `WebView` built without an explicit session
      creates the default one on the spot, and `get_network_session()` returns
      it — so this must run before the view exists, not after.
    - The web process takes them from the context it belongs to, through a
      construct-only property. There is no setter: it has to be a new context,
      handed to the view when the view is built.
    """
    settings = memory_pressure_settings(limit_mb)
    if settings is None:
        return None
    WebKit.NetworkSession.set_memory_pressure_settings(settings)
    return WebKit.WebContext(memory_pressure_settings=settings)


# --- Crash recovery --------------------------------------------------------


class CrashBackoff:
    """How long to wait before reloading a page whose web process died.

    One termination is an accident — a crash, or WebKit killing a leak at its
    memory limit — and the page is back after a second. Several in a few
    minutes is a page that dies as soon as it loads; reloading it every second
    would just keep a Doovit's CPU pinned on a panel nobody can read, so the
    wait grows to a minute. Once the page has stayed up for a while the history
    is forgotten, and the next crash is an accident again.

    Back to back, the waits run 1, 1, 5, 10, 20, 40, 60, 60, ... seconds.
    """

    FIRST_SECONDS = 1.0
    #: This many terminations inside `LOOP_WINDOW` is a crash loop.
    LOOP_COUNT = 3
    LOOP_WINDOW = 5 * 60
    STEP_SECONDS = float(RETRY_SECONDS)
    CAP_SECONDS = 60.0
    #: Up this long after loading, and the page counts as healthy again.
    STABLE_SECONDS = 10 * 60

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._recent: deque[float] = deque(maxlen=self.LOOP_COUNT)
        self._escalation = 0
        self._up_since: float | None = None

    def loaded(self) -> None:
        """The page finished loading. Only the first load since a crash counts."""
        if self._up_since is None:
            self._up_since = self._clock()

    def terminated(self) -> float:
        """Record a termination; return the seconds to wait before reloading."""
        now = self._clock()
        if self._up_since is not None and now - self._up_since >= self.STABLE_SECONDS:
            self._recent.clear()
            self._escalation = 0
        self._up_since = None
        self._recent.append(now)

        looping = (
            len(self._recent) == self.LOOP_COUNT
            and now - self._recent[0] <= self.LOOP_WINDOW
        )
        if not looping:
            return self.FIRST_SECONDS
        delay = min(self.CAP_SECONDS, self.STEP_SECONDS * 2**self._escalation)
        if delay < self.CAP_SECONDS:
            self._escalation += 1
        return delay


def termination_reason(reason) -> str:
    """`crashed`, `exceeded-memory-limit` or `terminated-by-api`: the enum's nick."""
    return getattr(reason, "value_nick", None) or str(reason)


# --- Reporting -------------------------------------------------------------

#: A status file past this size is started afresh rather than appended to. It
#: lives in the runtime dir under /tmp, which is RAM on these boards, and a
#: memory line a minute would otherwise grow it forever.
STATUS_MAX_BYTES = 64 * 1024


def report_status(line: str, path: str | None) -> None:
    """Tell the supervisor something, as `HMI-STATUS <line>`.

    Printed to stdout, where it lands in `docker logs`, and appended to the
    status file the supervising app reads. The file is the channel that
    matters: stdout is inherited from the compositor rather than piped to the
    app, because a pipe nobody drains blocks sway and takes the display down
    with it. Best-effort — nothing here may raise into GTK's main loop.
    """
    text = "HMI-STATUS " + " ".join(line.split())
    print(text, flush=True)
    if not path:
        return
    try:
        mode = "a"
        try:
            if os.path.getsize(path) > STATUS_MAX_BYTES:
                mode = "w"
        except OSError:
            pass  # not there yet; "a" creates it
        with open(path, mode) as fh:
            fh.write(text + "\n")
    except OSError as exc:
        log.debug("Could not write status to %s: %s", path, exc)


# --- Memory observation ----------------------------------------------------

PROC = Path("/proc")

#: /proc truncates a process name to 15 characters: "WebKitWebProcess"
#: reads as "WebKitWebProces", "WebKitNetworkProcess" as "WebKitNetworkPr".
WEB_PROCESS = "WebKitWebProc"
NETWORK_PROCESS = "WebKitNetwork"


def _descendants(root: int, proc: Path) -> dict[int, str]:
    """Every process below `root`, as pid -> name, from /proc/*/stat."""
    children: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue  # exited while we looked
        # "pid (comm) state ppid ...": comm may itself hold spaces or ")".
        close = stat.rfind(")")
        fields = stat[close + 2 :].split()
        if close < 0 or len(fields) < 2:
            continue
        pid = int(entry.name)
        names[pid] = stat[stat.find("(") + 1 : close]
        children.setdefault(int(fields[1]), []).append(pid)

    found: dict[int, str] = {}
    stack = list(children.get(root, []))
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found[pid] = names.get(pid, "")
        stack.extend(children.get(pid, []))
    return found


@dataclass(frozen=True)
class Usage:
    """One process kind's memory, in MiB.

    `rss` is what `top` shows. `private` (Private_Clean + Private_Dirty) is the
    part that is this process's alone, and `dirty` (Private_Dirty) is close to
    what WebKit's memory limit is judged against — RSS minus ~100 MB of shared
    libraries, give or take clean pages. None where smaps_rollup can't be read.
    """

    rss: float
    private: float | None = None
    dirty: float | None = None

    def __add__(self, other: Usage) -> Usage:
        def both(a, b):
            return None if a is None or b is None else a + b

        return Usage(
            self.rss + other.rss,
            both(self.private, other.private),
            both(self.dirty, other.dirty),
        )

    def describe(self) -> str:
        text = f"{self.rss:.0f} MiB rss"
        if self.private is not None and self.dirty is not None:
            text += f", {self.private:.0f} MiB private ({self.dirty:.0f} dirty)"
        return text


def _kb_fields(path: Path, wanted: tuple[str, ...]) -> dict[str, float]:
    """`Name:   1234 kB` lines out of a /proc file, as MiB, for the names asked."""
    found: dict[str, float] = {}
    try:
        for line in path.read_text().splitlines():
            name, _, rest = line.partition(":")
            if name in wanted:
                found[name] = int(rest.split()[0]) / 1024  # kB -> MiB
    except (OSError, ValueError, IndexError):
        pass
    return found


def _usage(pid: int, proc: Path) -> Usage | None:
    rss = _kb_fields(proc / str(pid) / "status", ("VmRSS",)).get("VmRSS")
    if rss is None:
        return None
    # smaps_rollup needs the same uid or root; the browser runs as root in the
    # container, but a process without it just reports RSS.
    rollup = _kb_fields(proc / str(pid) / "smaps_rollup", ("Private_Clean", "Private_Dirty"))
    if len(rollup) < 2:
        return Usage(rss)
    return Usage(rss, rollup["Private_Clean"] + rollup["Private_Dirty"], rollup["Private_Dirty"])


def webkit_memory(root: int, proc: Path = PROC) -> tuple[Usage | None, Usage | None]:
    """Memory of the web and network processes under `root`.

    None for a kind with no live process — between a crash and its reload,
    say. Walks descendants rather than children so a sandbox launcher in
    between would not hide them (the sandbox is off in this container, so today
    they are direct children).
    """
    web = network = None
    for pid, name in _descendants(root, proc).items():
        usage = _usage(pid, proc)
        if usage is None:
            continue
        if name.startswith(WEB_PROCESS):
            web = usage if web is None else web + usage
        elif name.startswith(NETWORK_PROCESS):
            network = usage if network is None else network + usage
    return web, network


def memory_status(web: Usage) -> str:
    """`memory <rss> [<private>]` — the second number only when it was readable."""
    if web.private is None:
        return f"memory {web.rss:.1f}"
    return f"memory {web.rss:.1f} {web.private:.1f}"


# --- The window ------------------------------------------------------------


class KioskWindow(Gtk.ApplicationWindow):
    def __init__(
        self,
        app: Gtk.Application,
        url: str,
        zoom: float,
        ignore_tls: bool,
        memory_limit_mb: int = 0,
        status_file: str | None = None,
    ):
        super().__init__(application=app)
        self.url = url
        self.status_file = status_file
        self._retry_source: int | None = None
        self._crash_source: int | None = None
        self._load_failed = False
        self._backoff = CrashBackoff()

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

        # Before the view: building it creates the default network session,
        # and the network process's memory settings only apply to sessions
        # created after they are set.
        context = limited_web_context(memory_limit_mb)
        if context is not None:
            log.info(
                "Memory limit %s MB of private footprint (not RSS): shedding from "
                "%.0f, killed and reloaded at %.0f",
                memory_limit_mb,
                memory_limit_mb * CONSERVATIVE_THRESHOLD,
                memory_limit_mb * KILL_THRESHOLD,
            )
            self.view = WebKit.WebView(settings=settings, web_context=context)
        else:
            log.info("No memory limit; WebKit's defaults apply")
            self.view = WebKit.WebView(settings=settings)

        self.view.set_zoom_level(zoom)
        self.view.connect("load-failed", self._on_load_failed)
        self.view.connect("load-changed", self._on_load_changed)
        self.view.connect("web-process-terminated", self._on_web_process_terminated)

        if ignore_tls:
            # Device-local pages are served over HTTPS with a self-signed
            # certificate; there is no CA to trust and no user to click through.
            self.view.get_network_session().set_tls_errors_policy(
                WebKit.TLSErrorsPolicy.IGNORE
            )

        self.set_child(self.view)
        self.load()

        GLib.timeout_add_seconds(MEMORY_REPORT_SECONDS, self._report_memory)

    def _status(self, line: str) -> None:
        report_status(line, self.status_file)

    def load(self) -> None:
        # Also the crash recovery: with no web process alive, a load launches
        # a new one. The configured URL rather than a reload, so a page that
        # had wandered off somewhere comes back to the one it should show.
        log.info("Loading %s", self.url)
        self.view.load_uri(self.url)

    def _on_load_changed(self, _view, event) -> None:
        if event == WebKit.LoadEvent.STARTED:
            self._load_failed = False
        elif event == WebKit.LoadEvent.FINISHED:
            # WebKit emits FINISHED after a failed load too; that one is not
            # "showing the page", and saying so would clear the error it caused.
            if self._load_failed:
                return
            log.info("Loaded %s", self.url)
            self._backoff.loaded()
            # Report readiness so the supervisor can tell "showing the page"
            # from "showing an error" without scraping pixels.
            self._status("loaded")

    def _on_load_failed(self, _view, _event, failing_uri, error) -> bool:
        self._load_failed = True
        log.warning("Load failed for %s: %s", failing_uri, error.message)
        self._status(f"failed {error.message}")
        self._schedule_retry()
        return True  # we handled it; don't show WebKit's own error page

    def _schedule_retry(self) -> None:
        # A crash reload already pending owns the next load; retrying on the
        # five-second clock as well would defeat its backoff.
        if self._retry_source is not None or self._crash_source is not None:
            return

        def retry() -> bool:
            self._retry_source = None
            self.load()
            return GLib.SOURCE_REMOVE

        self._retry_source = GLib.timeout_add_seconds(RETRY_SECONDS, retry)

    def _on_web_process_terminated(self, _view, reason) -> None:
        """The page's web process is gone: crashed, or killed at its memory limit.

        Without this the view stays blank forever. Reloading from inside the
        signal handler is avoided on purpose — the view is mid-teardown — so
        the load goes on a timer, which is also where the backoff lives.
        """
        name = termination_reason(reason)
        delay = self._backoff.terminated()
        log.warning(
            "The page's web process terminated (%s); reloading in %.0f s", name, delay
        )
        self._status(f"terminated {name}")

        if self._retry_source is not None:
            GLib.source_remove(self._retry_source)
            self._retry_source = None
        if self._crash_source is not None:
            return

        def reload() -> bool:
            self._crash_source = None
            self.load()
            return GLib.SOURCE_REMOVE

        self._crash_source = GLib.timeout_add(int(delay * 1000), reload)

    def _report_memory(self) -> bool:
        try:
            web, network = webkit_memory(os.getpid())
            if web is None:
                log.info("Memory: no web process running")
            else:
                log.info(
                    "Memory: web process %s; network process %s",
                    web.describe(),
                    network.describe() if network is not None else "not running",
                )
                self._status(memory_status(web))
        except Exception:  # noqa: BLE001 — observation must never take the page down
            log.debug("Could not read WebKit's memory", exc_info=True)
        return GLib.SOURCE_CONTINUE

    def reload_now(self) -> None:
        """Re-fetch the page and everything under it.

        Bypassing the cache is the point: the usual reason to be asked is that
        the app serving this page has just been redeployed underneath it, so a
        revalidating reload could put the old bundle straight back up.
        """
        log.info("Reloading %s", self.url)
        self.view.reload_bypass_cache()


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--memory-limit-mb",
        type=int,
        default=0,
        help="Memory-pressure limit for WebKit's web and network processes, in "
        "MB of private footprint (not RSS); 0 leaves WebKit's defaults (no "
        "limit, never killed).",
    )
    parser.add_argument(
        "--status-file",
        default=None,
        help="Also append HMI-STATUS lines here, for the supervising app.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    args = build_parser().parse_args(argv)

    app = Gtk.Application(application_id="com.doover.hmi")
    window: dict[str, KioskWindow] = {}

    def on_activate(application: Gtk.Application) -> None:
        win = KioskWindow(
            application,
            args.url,
            args.zoom,
            args.ignore_tls,
            memory_limit_mb=args.memory_limit_mb,
            status_file=args.status_file,
        )
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
