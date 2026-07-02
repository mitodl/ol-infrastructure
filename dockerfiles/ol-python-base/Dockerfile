# hadolint global ignore=DL3008
# Shared Python base image for MIT Open Learning services.
#
# Bakes the substrate that every Python app Dockerfile previously duplicated:
# slim Python, the common-core apt packages, the uv binary, a non-root `mitodl`
# user, and the /opt/venv environment configuration. App images do
# `FROM mitodl/ol-python-base:<python-version>` and add only their app-specific
# layers (extra apt, node build stages, project source, run command).
#
# Build:
#   docker build --build-arg PYTHON_VERSION=3.12 -t mitodl/ol-python-base:3.12 .
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

LABEL maintainer="ODL DevOps <mitx-devops@mit.edu>"

# Common-core apt packages shared across the Python services. App-specific
# extras (chromium, libcairo2-dev, operator tooling, ...) stay in each app's
# own Dockerfile / apt.txt.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      git \
      libffi-dev \
      libjpeg-dev \
      libpq-dev \
      libxmlsec1-dev \
      net-tools \
      pkg-config \
      postgresql-client \
      zlib1g-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install uv (pinned via the upstream tag; bump deliberately).
COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /uvx /usr/local/bin/

# Non-root runtime user and the directories the apps expect to own.
RUN adduser --disabled-password --gecos "" mitodl && \
    mkdir -p /src /opt/venv /var/media && \
    chown -R mitodl:mitodl /src /opt/venv /var/media

# Python + uv environment. UV_CACHE_DIR points at a stable location so app
# builds can mount a BuildKit cache there for fast `uv sync`.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/opt/uv-cache \
    PATH=/opt/venv/bin:$PATH

RUN mkdir -p /opt/uv-cache && chown -R mitodl:mitodl /opt/uv-cache

WORKDIR /src
