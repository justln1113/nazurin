ARG PYTHON_VERSION=3.10
ARG FFMPEG_VERSION=9.0.2-1

# FFmpeg
FROM mwader/static-ffmpeg:${FFMPEG_VERSION} AS ffmpeg

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

# Runtime
FROM python:${PYTHON_VERSION}-alpine

# Keeps Python from generating .pyc files in the container
ENV PYTHONDONTWRITEBYTECODE=1

# Turns off buffering for easier container logging
ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache curl

# Install FFmpeg
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
RUN ffmpeg -version && ffprobe -version

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
