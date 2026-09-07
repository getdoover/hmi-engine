# HMI Engine

Show a web page on a Linux device's own display, and keep it there.

The app brings its own graphics session — compositor, browser and software
renderer — because the devices this targets generally have none. A typical
industrial gateway has a KMS-capable kernel and nothing above it: no X, no
Wayland, no browser, often no usable GPU userspace, and a read-only or
package-less root filesystem. Rather than ask the device for a desktop, this
ships one in the container.

## What it does

```
hmi-engine (this app, supervising)
  └── sway                     wlroots compositor, output mode pinned
        └── WebKitGTK window   one URL, fullscreen, no chrome
```

...unless the device already has a compositor of its own, in which case the
middle row is somebody else's and this app supervises only the browser:

```
hmi-engine (this app, supervising)
  └── WebKitGTK window         attached to the host's Wayland socket
```

Both are detected, never configured. See [Devices that already have a
desktop](#devices-that-already-have-a-desktop).

Everything about the display is detected at startup:

| Decision | How it's made |
|---|---|
| Which output | First connected connector under `/sys/class/drm`, or the one named in config |
| Which DRM device | The card that owns that connector — not assumed to be `card0` |
| Which mode | The connector's preferred mode, or the one configured |
| GPU or software | Mesa present **and** a render node **and** no vendor-only driver → GL; otherwise pixman |

That last row is the one that matters in the field. A board can have a GPU the
kernel exposes and Mesa cannot drive — an i.MX8 with NXP's `galcore` is the
common case — and asking for GL there gets a compositor that refuses to start.
Detection is deliberately pessimistic: when unsure it picks software rendering,
which always works.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `url` | auto | The page to display. Blank means "the widget of the app that asked for a screen"; the install name of a widget app picks between several; anything else is a URL or template — see below |
| `zoom` | `1.0` | Page zoom. Below 1 fits a desktop layout onto a small panel |
| `output` | auto | Connector, e.g. `HDMI-A-1` |
| `mode` | preferred | e.g. `1280x720@60`. Driving a 1080p panel at 720p roughly halves the work with no GPU |
| `rotation` | `0` | 0 / 90 / 180 / 270, for a panel mounted sideways |
| `renderer` | `auto` | Force `gl` or `pixman` if detection guesses wrong |
| `reload_interval_min` | `0` | Periodic reload; guards against a page that wedges after weeks |
| `hide_cursor` | `true` | There is rarely a mouse |
| `ignore_tls_errors` | `true` | Device-local pages use self-signed certificates |
| `conflicting_services` | — | Init scripts to stop first (see below) |

## Devices that already have a desktop

A Raspberry Pi running Raspberry Pi OS is the opposite of the bare gateway
above: lightdm has already autologged into a labwc session that holds DRM
master on the connector. Two compositors cannot drive one output, and the
incumbent wins — so starting sway there gets

```
[ERROR] [sway/config/output.c:897] Requested backend configuration failed
[ERROR] [sway/tree/view.c:623] select_workspace:Expected to find a workspace
Gdk-Message: Lost connection to Wayland compositor.
```

every five seconds, forever, with the panel never showing anything.

So at startup the app looks for a compositor already running on the host — a
live socket under `/run/user/<uid>/wayland-*` — and if it finds one, attaches
the browser to it and starts no compositor of its own. The `compositor` tag
reports which happened, `own` or `host`.

Nothing on the host is stopped, disabled or reconfigured to make this work. The
runtime directory is mounted **read-only**, and `WAYLAND_DISPLAY` is set to the
socket's absolute path so `XDG_RUNTIME_DIR` can stay pointed at the container's
own writable directory. Removing the app leaves the device exactly as it was.

A socket that exists but refuses a connection is a crashed session's leftover
and is skipped. Where several sessions have one, a logged-in user's is
preferred over a system account's, which is usually a greeter.

In this mode the display belongs to the host, so `output`, `mode`, `rotation`
and `renderer` do nothing — the app logs which of them you had set rather than
appearing to ignore you. Set them on the host's own session instead.

## Putting a widget app on the panel

An app with a dashboard widget can put itself on the device's own panel with one
line in its `doover_config.json`:

```json
"depends_on": ["hmi_engine"]
```

Installing that app now installs an HMI engine beside it — the platform creates an
install for everything in `depends_on`. That install arrives with **no config**,
because there is nowhere for a dependent's config to come from, so the engine
works out what to show instead of being told:

1. It reads the device's `deployment_config` aggregate, which holds every
   install on the device.
2. The platform stamps `dv_widget_url` into an entry only when that application
   ships a widget, and sets it to the widget's channel name. That flag is the
   whole detection rule — there is nothing for the widget repo to declare
   beyond the dependency, and nothing to keep in sync.
3. The URL becomes
   `https://localhost:49100/widget/<channel>?app_key=<install>`.

The page is served by the **device agent**, not the cloud, so there is no login
for a keyboard-less panel to get past and no dependence on the device having a
connection at the moment someone walks past it. It is HTTPS with a self-signed
certificate, which is what `ignore_tls_errors` defaults to true for.

Order of deployment doesn't matter. An install that starts before its widget app
has published its config finds nothing, says so on the `last_error` tag, and the
watchdog picks it up on the next cycle.

Anything about the screen — zoom, mode, rotation — stays on the engine install,
where the panel is. The widget app doesn't get an opinion about hardware it
can't see.

Several widget apps on one device is the one case a default can't decide. The
engine says so on `last_error` — naming the installs it found — rather than
guessing, and the answer is to put the install name of the one you want in
`url`:

```
Several widget apps here; set URL to the install name of the one you want,
e.g. data_report_segmenter_1. They are: data_report_segmenter_1,
petronash_hmi_1, petronash_pump_controller_1
```

The instruction comes first because the tag is cut to 200 characters, and a
device with a handful of widget apps writes more than that.

### What `url` accepts

One knob, three forms:

| You write | You get |
|---|---|
| nothing | the widget of the app that pulled this one in — the normal case |
| `petronash_hmi_1` | that install's widget. The application name (`petronash_hmi`) works too, when only one install of it is here |
| `http://…` or a template | exactly that, with any placeholders expanded |

A name is anything with no scheme, path, placeholder or space in it, so a
written-out URL is never taken for a name. The reverse has one hole: a
scheme-less address such as `192.168.1.50` or `dashboard.local` looks exactly
like a name and is read as one — give a page its `http://` and it goes to the
browser (which wouldn't have loaded it without one either).

A name that isn't a widget app here is reported on `last_error` with the ones
that are, rather than being handed to the browser — which is what used to
happen, and got "The URL can't be shown" every five seconds on a blank panel.

### URL templates

A configured `url` may use `{device_agent_url}`, `{widget_channel}`, `{app_key}`,
`{agent_id}` and `{org_id}`, which is what lets one config profile — or one
Solution — cover a fleet instead of a single device. `{device_agent_url}` is
fixed at `https://localhost:49100`, the agent's default web port:

```
{device_agent_url}/widget/{widget_channel}?app_key={app_key}   # the default
http://localhost:8080                                          # another app's own UI
```

A URL with no placeholders never touches the aggregate, so a device-local page
works on a device that has deployed nothing else.

## Redeploying a widget updates the panel

Deploying the widget app republishes its bundle to its widget channel — the same
channel the page is served from. The engine subscribes to that channel and to
nothing else, so new JavaScript landing *is* the trigger: a second later the
browser is sent `SIGHUP` and reloads, bypassing its cache so the new build can't
be served from the old one. The compositor stays up, so the panel never blanks.

Nothing else causes a reload. Redeploying an unrelated app on the same device
leaves the page alone, and the engine's own config needs no watching — editing an
install's config redeploys it, and redeploying this app restarts the container
with the new config already in hand.

The browser is started by the compositor, not by this app, so it is found by
scanning `/proc` for `hmi_browser.py` rather than kept as a handle. If no
browser answers, the session is restarted rather than left showing a stale page.

## Vendor splash screens

Some vendor images run their own status screen on the framebuffer and will
repaint over anything else — the symptom is your page appearing for a moment and
being replaced a second later. Name the init scripts in `conflicting_services`
and they are stopped before the session starts.

On an ELPRO Quantum that's `S01splash` **and** `S89splash` — the same Qt splash
registered twice, and stopping only one leaves the other to fight you.

Stopping them at boot is a separate, permanent change to the device and is left
to the operator; this app only stops them while it runs. Doing it permanently
means renaming them out of `rcS`'s `S??*` glob, since Buildroot's `rcS` runs
each match without checking the execute bit:

```sh
mv /etc/init.d/S89splash /etc/init.d/disabled.S89splash
```

## Requirements

The container needs real access to the display hardware, declared in
`deployment/docker-compose.yml` — the platform ships that with the app, so an
install gets it automatically. Without it the app starts, finds the connector,
and then cannot open it, which reads as an app bug rather than a missing
permission.

```yaml
privileged: true                        # DRM master
volumes:
  - /dev/dri:/dev/dri:rw                # display and render nodes
  - /run/udev:/run/udev:ro              # wlroots output discovery
  - /run/user:/run/user:ro              # find the host's compositor, if it has one
  - /etc/init.d:/host/etc/init.d:ro     # for conflicting_services
```

An install created before this mount existed will keep fighting the host's
compositor until it is redeployed, because the app cannot see a socket that was
never mounted in. Redeploying picks up the current compose.

## Development

```bash
uv sync
uv run pytest                       # detection and config-generation logic
uv run export-config && uv run export-ui
docker buildx build --platform linux/arm64 -t hmi-engine .
```

The browser deliberately runs on the **distro** Python rather than the app venv:
PyGObject is a compiled extension built against the distro interpreter, and the
venv is on a different minor version. The supervising app keeps its venv; the
window it launches gets a standalone script and the interpreter that can load
`_gi`.

## Project map

| Path | Purpose |
|---|---|
| `src/hmi_engine/source.py` | Which app on this device wanted a screen, and the URL that shows it |
| `src/hmi_engine/display.py` | Detection — connector, card, modes, whether Mesa can help |
| `src/hmi_engine/session.py` | Compositor config generation and process supervision |
| `src/hmi_engine/browser.py` | The fullscreen WebKit window (standalone, distro Python) |
| `src/hmi_engine/application.py` | Doover app: config, tags, UI, watchdog |
| `tests/` | Detection and config-generation, which have to cope with unfamiliar hardware |

## Relationship to `doover-kiosk`

`doover-kiosk` is the apt package for Raspberry Pi devices, running WebKitGTK
under labwc on the Pi desktop. It is more capable on that hardware — autologin,
reload button, memory watchdog, sticky settings — and a Pi has a working GPU and
a package manager, so it needs none of what this app carries.

This app exists for devices where that isn't true, and for fleets that would
rather configure a display from the Doover UI than over SSH. The two share an
engine choice (WebKitGTK) but not a delivery mechanism.
