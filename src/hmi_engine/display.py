"""Work out what to draw on, and how, without being told.

A display app that has to be handed the card, the connector and the renderer
only works on the board it was written for. Everything here is read
from sysfs at startup so the same image runs on a Raspberry Pi with a VideoCore
GPU, an i.MX8 with a vendor driver Mesa cannot use, and a QEMU guest with no GPU
at all.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DRM_CLASS = Path("/sys/class/drm")
DRI_DIR = Path("/dev/dri")

#: Where distributions put Mesa's DRI drivers. Alpine and Debian disagree, and
#: `LIBGL_DRIVERS_PATH` has to be right or EGL silently falls back to nothing.
DRI_SEARCH_PATHS = (
    "/usr/lib/xorg/modules/dri",
    "/usr/lib/dri",
    "/usr/lib/aarch64-linux-gnu/dri",
    "/usr/lib/x86_64-linux-gnu/dri",
    "/usr/lib/arm-linux-gnueabihf/dri",
)


@dataclass(frozen=True)
class Mode:
    width: int
    height: int
    refresh: int | None = None

    def __str__(self) -> str:
        return f"{self.width}x{self.height}" + (f"@{self.refresh}Hz" if self.refresh else "")

    @classmethod
    def parse(cls, text: str) -> "Mode | None":
        """Accept `1280x720`, `1280x720@60` or `1280x720@60Hz`."""
        match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)(?:@(\d+)(?:Hz)?)?\s*", text or "")
        if not match:
            return None
        w, h, hz = match.groups()
        return cls(int(w), int(h), int(hz) if hz else None)


@dataclass(frozen=True)
class Display:
    #: Connector name as both sysfs and wlroots know it, e.g. "HDMI-A-1".
    connector: str
    #: The DRM device that drives it, e.g. "/dev/dri/card1".
    device: str
    modes: tuple[Mode, ...]
    #: True when Mesa can render on this system; False means software (pixman).
    accelerated: bool
    dri_path: str | None

    @property
    def preferred(self) -> Mode | None:
        # sysfs lists modes best-first, which is the connector's own preference.
        return self.modes[0] if self.modes else None

    def supports(self, mode: Mode) -> bool:
        return any(m.width == mode.width and m.height == mode.height for m in self.modes)


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _card_device(connector_dir: Path) -> str | None:
    """Map `card1-HDMI-A-1` back to the `/dev/dri/cardN` that owns it."""
    name = connector_dir.name.split("-", 1)[0]
    node = DRI_DIR / name
    return str(node) if node.exists() else None


def _parse_modes(connector_dir: Path) -> tuple[Mode, ...]:
    modes = []
    for line in _read(connector_dir / "modes").splitlines():
        mode = Mode.parse(line)
        if mode and mode not in modes:
            modes.append(mode)
    return tuple(modes)


def find_dri_path() -> str | None:
    """The directory holding Mesa's `*_dri.so`, if any is installed."""
    for candidate in DRI_SEARCH_PATHS:
        path = Path(candidate)
        if path.is_dir() and any(path.glob("*_dri.so")):
            return candidate
    return None


def has_render_node() -> bool:
    """A `renderD*` node means *some* GPU is exposed — not that Mesa can use it.

    An i.MX8 with NXP's `galcore` driver has one and Mesa cannot drive it, so
    this is only ever a hint; `probe_acceleration` makes the real decision.
    """
    return DRI_DIR.is_dir() and any(DRI_DIR.glob("renderD*"))


def probe_acceleration(dri_path: str | None) -> bool:
    """Decide whether to ask the compositor for GL or for software rendering.

    Mesa has to be installed *and* there has to be a render node it might drive.
    Getting this wrong in the optimistic direction costs a compositor that
    refuses to start, so when in doubt this says no and the caller falls back to
    pixman, which always works.
    """
    if not dri_path or not has_render_node():
        return False

    # Vendor kernel drivers that ship their own closed userspace: Mesa has a
    # kernel node to look at but no driver that can talk to it.
    vendor_only = {"galcore"}
    for card in DRM_CLASS.glob("card*"):
        driver = os.path.basename(os.path.realpath(card / "device" / "driver"))
        if driver in vendor_only:
            log.info("Found vendor-only GPU driver %r; using software rendering", driver)
            return False
    return True


def detect(preferred_connector: str = "") -> Display | None:
    """Find a connected display, preferring the named connector if given."""
    if not DRM_CLASS.is_dir():
        log.warning("No %s — this kernel has no DRM/KMS", DRM_CLASS)
        return None

    connected = []
    for entry in sorted(DRM_CLASS.glob("card*-*")):
        if _read(entry / "status") != "connected":
            continue
        device = _card_device(entry)
        if not device:
            continue
        # `card1-HDMI-A-1` → `HDMI-A-1`, which is what wlroots calls it.
        connector = entry.name.split("-", 1)[1]
        connected.append((connector, device, _parse_modes(entry)))

    if not connected:
        log.warning("No connected display found under %s", DRM_CLASS)
        return None

    chosen = None
    if preferred_connector:
        chosen = next((c for c in connected if c[0] == preferred_connector), None)
        if chosen is None:
            log.warning(
                "Configured output %r is not connected; falling back to %s",
                preferred_connector,
                connected[0][0],
            )
    if chosen is None:
        chosen = connected[0]

    connector, device, modes = chosen
    dri_path = find_dri_path()
    return Display(
        connector=connector,
        device=device,
        modes=modes,
        accelerated=probe_acceleration(dri_path),
        dri_path=dri_path,
    )


def resolve_mode(display: Display, requested: str) -> Mode | None:
    """Pick the mode to drive the panel at.

    An unsupported request is honoured anyway — some panels accept modes they do
    not advertise — but it is logged, because a black screen is otherwise a
    mystery.
    """
    if not requested:
        return display.preferred

    mode = Mode.parse(requested)
    if mode is None:
        log.warning("Could not parse mode %r; using the connector's preferred mode", requested)
        return display.preferred
    if not display.supports(mode):
        log.warning(
            "%s does not advertise %s (it offers %s); trying it anyway",
            display.connector,
            mode,
            ", ".join(str(m) for m in display.modes[:6]) or "nothing",
        )
    return mode
