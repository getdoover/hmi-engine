# HMI Engine

A Doover device app that shows a web page on the device's own display. It brings
its own compositor and browser because the target devices have neither.

## Commands

```bash
uv sync
uv run pytest                                    # detection + config generation
uv run export-config && uv run export-ui         # -> doover_config.json
docker buildx build --platform linux/arm64 -t hmi-engine .
```

## Structure

```
src/hmi_engine/display.py      # detect connector, card, modes, GPU, host compositor
src/hmi_engine/session.py      # sway config generation + process supervision
                               #   (own compositor, or just the browser on the host's)
src/hmi_engine/browser.py      # fullscreen WebKitGTK window (standalone)
src/hmi_engine/application.py  # Doover app: config, tags, UI, watchdog
src/hmi_engine/source.py       # widget-app discovery + URL templating
```

## Things that will bite you

- **A blank URL is the normal case, not a broken install.** A widget app makes
  itself an HMI panel with `"depends_on": ["hmi_engine"]`; the platform then
  creates the engine install *bare*, because a dependent install has nowhere to
  get config from (`ApplicationInstallation.save` in doover-control). So the
  engine finds its own page: it reads the device's `deployment_config` aggregate
  and looks for an entry carrying `dv_widget_url`, which the platform sets only
  for apps that ship a widget. Don't move that declaration into the widget's
  own config — the point is that a widget repo adds one line and nothing else.
- **Widgets are served locally, by the DDA, not by the cloud site.**
  `dv_widget_url` is the widget's *channel name*, and the agent's web server
  (`49100` by default) serves it at `/widget/<channel>?app_key=<install>`. That
  is why a keyboard-less panel never meets a login screen — don't "fix" the
  default URL to point at `<org>.doover.com/agent/<id>`, which is behind
  FusionAuth.
- **One knob for what to show, and it is `url`.** There is deliberately no
  "device agent URL" or "source app" setting: the agent's web port is fixed at
  49100, and the one case a default can't decide — two widget apps, one panel —
  is answered by writing the URL out. Adding a config element to cover an edge
  case makes the common install look like it has decisions to make.
- **A config element's key comes from its display name, not the attribute**
  (`config.String("Reload Interval (min)")` exports as `reload_interval_min`,
  not `reload_minutes`). That mismatch survives only because nothing references
  it by key; keep new elements' names and display names aligned.
- **`self.config` is only this app's entry.** The rest of the device lives in
  the `deployment_config` aggregate, fetched via `device_agent`. Every entry
  carries `AGENT_ID`, `ORGANISATION_ID`, `APPLICATION_NAME` and `APP_KEY`.
- **Anything unknown at startup raises `UnresolvedURL`, never an exception.**
  An install deployed before its widget app, or a failed aggregate fetch, both
  mean "try again shortly" — the watchdog does exactly that. Letting it escape
  `setup()` takes the app down instead of self-healing.
- **One subscription: the widget channel.** It carries the bundle, so an update
  there means the page itself changed — SIGHUP the browser and it reloads
  without the panel blanking. Don't add a `deployment_config` subscription back
  for config changes: editing an install's config redeploys it, and redeploying
  this app restarts the container with the new config already applied. Watching
  it would only flicker the wall every time an unrelated app on the device is
  redeployed.
- **Subscriptions can't be removed** (`add_event_callback` has no inverse), so
  the widget-channel handler compares the channel it was woken for against the
  one currently on screen and no-ops on a stale one.
- **The browser is a grandchild, found via `/proc`.** sway launches it with
  `exec_always`, so there is no handle — `session.browser_pids` scans for
  `hmi_browser.py`. Keep it dependency-free; adding procps to the image just
  to run `pkill` is a bigger change than it looks (see the file-capabilities
  note below).
- **Two Pythons, on purpose.** PyGObject is compiled against the distro
  interpreter; the app venv is a different minor version and cannot load `_gi`.
  The browser is copied to `/usr/local/lib/hmi_browser.py` and run with
  `/usr/bin/python3`. Don't "tidy" it into the venv.
- **Never assume `card0`.** The DRM card that owns the connected connector is
  whichever `cardN-<connector>` sysfs entry says; on an i.MX8 the display pipe
  is `card1` and `card0` is the GPU.
- **A render node does not mean Mesa can use it.** i.MX8 exposes `renderD128`
  through NXP's `galcore`, which Mesa cannot drive. `probe_acceleration`
  blacklists vendor-only drivers and falls back to pixman. Guessing optimistically
  gets a compositor that won't start.
- **sway, not cage.** cage is smaller but cannot pin an output mode, and driving
  a 1080p panel at 720p is the single biggest saving when rendering in software.
- **A host that already has a compositor gets used, not evicted.** On Raspberry
  Pi OS, lightdm autologs into labwc, which holds DRM master; sway then fails
  its output config, finds no workspace, and the watchdog restarts it every
  five seconds forever (`find_host_compositor` exists because a device was found
  on attempt 122). Where a live socket turns up under `/run/user/<uid>/`, the
  browser attaches to it and no compositor is started. Nothing on the host is
  stopped or disabled to make room — that was considered and rejected: a
  display manager disabled from inside a container is permanent, survives the
  app's removal, and would break every Pi running the `doover-kiosk` apt
  package, which *depends* on labwc. `stop_conflicting_services` is as far as
  this app goes into the host, and it only ever stops something transiently.
- **In hosted mode `output`, `mode`, `rotation` and `renderer` mean nothing** —
  the host's compositor owns all four. They are logged as ignored rather than
  silently dropped, because a panel that is the wrong way up after someone set
  `rotation` otherwise looks like the setting is broken.
- **`WAYLAND_DISPLAY` is an absolute path, deliberately.** libwayland skips
  `XDG_RUNTIME_DIR` entirely when the value starts with `/`, which is what lets
  `/run/user` be mounted read-only while the browser still has a writable
  runtime dir of its own. Setting `XDG_RUNTIME_DIR` to the host's directory
  instead works too, but then the app is writing into someone else's session.
- **cog's DRM backend segfaults on imx-drm** importing dmabuf, and Alpine 3.23
  dropped the cog package anyway. WebKitGTK under sway is the combination that
  works.
- **File capabilities break `docker load`** on filesystems that can't store
  `security.*` xattrs (the Quantum's overlay) — and it fails the whole image, not
  just the file. sway and gstreamer's PTP helper both ship them. The Dockerfile
  strips every capability with `getcap -r / | setcap -r` **inside the same RUN as
  the apk add**. Layers are additive: stripping in a later RUN leaves the xattr
  in the layer underneath and `docker load` still trips over it. Verify with
  `docker save img | grep -c security.capability` — it must be 0.
- **Vendor splashes fight for the framebuffer.** Symptom is the page showing then
  being replaced a second later. See `conflicting_services` in the README.

## Getting the image onto a device by hand

Publish to a registry and `docker pull` where you can. When you must side-load,
stream it — do not stage a tarball on the device first:

```sh
docker save hmi-engine:test | gzip -1 | \
  ssh root@device 'docker load'
```

`/tmp` is tmpfs on these boards. A 250 MB tarball written there is 250 MB of a
3.6 GB device's RAM, and doing it while the kiosk browser is running was enough
to starve sshd on a Quantum already at load 9 — the box stayed pingable and
stopped answering SSH. Stream into `docker load`, or stage on `/config`, which
is real storage.
