# Running Stamp's tests without a desktop.
#
# Sixteen of the tests build a real window, and two of those build the
# OpenCascade viewport, which needs a genuine GL surface - Qt's "offscreen"
# platform hangs on it, and its empty font database breaks the text tests as
# well.  So the tests need a display; they just do not need *yours*.
#
# This gives them one: Xvfb with Mesa's software renderer, inside a container.
# Nothing appears on the host, nothing steals focus, and a run that wedges takes
# the container with it rather than the desktop.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    # Mesa's software rasteriser.  There is no GPU in here, and OpenCascade
    # needs a real GL context rather than a fast one.
    LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe \
    QT_QPA_PLATFORM=xcb

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11-utils xauth \
        libgl1 libglu1-mesa libegl1 libglx-mesa0 libopengl0 \
        libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
        libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
        libxcb-xinerama0 libxcb-xkb1 libxrender1 libxi6 libsm6 libice6 \
        libfontconfig1 libfreetype6 libdbus-1-3 \
        libglib2.0-0 libxkbcommon0 libnss3 libxcomposite1 libxdamage1 \
        libxrandr2 libxtst6 libasound2 libexpat1 \
        fonts-dejavu-core fonts-liberation \
        curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /stamp

# Dependencies first, so editing source does not re-resolve a 500 MB wheel set.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/stamp/__init__.py src/stamp/__init__.py
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

# -a picks a free display number, so two runs at once do not collide.
# --no-sync matters: without it `uv run` re-resolves the project on every start,
# reaches for the network, and inside the container that hangs indefinitely with
# no output - which looked exactly like a wedged test suite and is not one.
ENTRYPOINT ["xvfb-run", "-a", "--server-args=-screen 0 1600x1200x24", \
            "uv", "run", "--no-sync", "pytest"]
CMD []
