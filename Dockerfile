FROM spaneng/doover_device_base AS base_image
LABEL com.doover.app="true"
LABEL com.doover.managed="true"
HEALTHCHECK --interval=30s --timeout=2s --start-period=5s CMD curl -f "127.0.0.1:$HEALTHCHECK_PORT" || exit 1

## FIRST STAGE ##
FROM base_image AS builder

COPY --from=ghcr.io/astral-sh/uv:0.7.3 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN uv venv --system-site-packages
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    SKIP=$(uv pip freeze --system | sed -E 's/[[:space:]@=].*//; s/^/--no-install-package /' | tr '\n' ' ') && \
    uv sync --locked --no-install-project --no-dev $SKIP

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    SKIP=$(uv pip freeze --system | sed -E 's/[[:space:]@=].*//; s/^/--no-install-package /' | tr '\n' ' ') && \
    uv sync --locked --no-dev $SKIP


## SECOND STAGE ##
FROM base_image AS final_image

# The graphics session, which the host is not expected to provide. Devices this
# runs on typically have a KMS-capable kernel and nothing above it: no X, no
# Wayland, no browser, and often no GPU userspace at all.
#
#   sway/seatd          compositor; sway specifically because it can pin an
#                       output mode, which is how a 1080p panel gets driven at
#                       720p to save a software renderer half the work
#   mesa-dri-gallium    llvmpipe + kms_swrast, so there is always a renderer
#   webkit2gtk-6.0      the browser engine, via GTK4
#   py3-gobject3        how the kiosk window drives it
#   bubblewrap          WebKit's sandbox helper
#   font-*              a device image has no fonts; without these the page
#                       renders as empty boxes
RUN apk add --no-cache \
        sway \
        swaybg \
        seatd \
        mesa-dri-gallium \
        mesa-egl \
        mesa-gles \
        mesa-gbm \
        gtk4.0 \
        webkit2gtk-6.0 \
        py3-gobject3 \
        bubblewrap \
        font-dejavu \
        font-noto \
        libcap-utils \
    # Strip every file capability, in this same layer.
    #
    # sway and gstreamer's PTP helper ship `security.capability` xattrs. Some
    # device filesystems cannot store them — an overlayfs on an ELPRO Quantum,
    # for one — and `docker load` then fails the *whole image*, not just the
    # file. It has to happen here rather than in a later RUN: layers are
    # additive, so stripping afterwards leaves the original xattr in the layer
    # underneath and load still trips over it. Nothing needs these caps; the
    # app runs the compositor as root.
    && getcap -r / 2>/dev/null | cut -d" " -f1 | while read -r f; do \
         [ -n "$f" ] && setcap -r "$f" 2>/dev/null || true; \
       done \
    && apk del libcap-utils \
    && rm -rf /var/cache/apk/*

COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH"

# The browser runs on the distro's Python, not the app venv: PyGObject is a
# compiled extension built against the distro interpreter, and the venv is on a
# different minor version. The supervising app keeps its venv; the window it
# launches gets a standalone script and the interpreter that can load `_gi`.
COPY src/hmi_engine/browser.py /usr/local/lib/hmi_browser.py

CMD ["doover-app-run"]
