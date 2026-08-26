ARG PYTHON_VERSION=3.10

# Builder
FROM python:${PYTHON_VERSION}-alpine AS builder

ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apk add --update git build-base libffi-dev curl-dev

WORKDIR /app

# Install requirements
# NOTE: Synology Container Manager ships without the buildx plugin, so its
# builds fall back to the legacy builder, which doesn't understand
# `RUN --mount` (BuildKit-only). Use plain COPY + RUN instead: functionally
# equivalent, only losing uv's download cache between builds.
COPY uv.lock pyproject.toml ./
RUN uv sync --frozen --no-install-project --no-dev

# Install FFmpeg
ARG TARGETARCH
ARG FFMPEG_VERSION=4.2.2

# TARGETARCH is only auto-populated by BuildKit; the legacy builder leaves it
# empty, which would produce a 404 URL. Fall back to detecting the build
# machine's architecture, which is always correct for a non-BuildKit build
# since those can't cross-compile anyway.
RUN ARCH="${TARGETARCH:-$(apk --print-arch | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')}" && \
    echo "Download from https://www.johnvansickle.com/ffmpeg/old-releases/ffmpeg-${FFMPEG_VERSION}-${ARCH}-static.tar.xz" && \
    wget https://www.johnvansickle.com/ffmpeg/old-releases/ffmpeg-${FFMPEG_VERSION}-${ARCH}-static.tar.xz -O ffmpeg.tar.xz && \
    tar Jxvf ./ffmpeg.tar.xz && \
    cp ./ffmpeg-${FFMPEG_VERSION}-${ARCH}-static/ffmpeg /usr/local/bin/

# Runtime
FROM python:${PYTHON_VERSION}-alpine

# Keeps Python from generating .pyc files in the container
ENV PYTHONDONTWRITEBYTECODE=1

# Turns off buffering for easier container logging
ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache curl

# Install FFmpeg
COPY --from=builder /usr/local/bin/ffmpeg /usr/local/bin/

# NOTE: upstream copies cURL Impersonate binaries from /usr/local here, but
# nothing in the builder stage ever puts them there -- curl-cffi vendors its
# native library inside the wheel (site-packages/curl_cffi). Verified against a
# working image: neither path exists in it, yet `import curl_cffi` succeeds.
# BuildKit silently tolerates a glob that matches nothing; the legacy builder
# (Synology Container Manager) fails with "no source files were specified",
# so these no-op COPY lines are dropped.

# Copy pip requirements
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY nazurin ./nazurin

# Creates a non-root user with an explicit UID and adds permission to access the /app folder
RUN adduser -u 5678 --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

CMD ["python", "-m", "nazurin"]
