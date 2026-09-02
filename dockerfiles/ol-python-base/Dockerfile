# hadolint global ignore=DL3008
# Shared Python base image for MIT Open Learning services.
#
# Bakes the substrate that every Python app Dockerfile previously duplicated:
# hardened Python, the common-core apt packages, the uv binary, a non-root
# `mitodl` user, and the /opt/venv environment configuration. App images do
# `FROM mitodl/ol-python-base:<python-version>` and add only their app-specific
# layers (extra apt, node build stages, project source, run command).
#
# The base is Docker Hardened Images' Python `-dev` variant (Debian 13/trixie):
# near-zero-CVE, continuously rebuilt, signed provenance. The `-dev` variant is
# deliberate — it keeps the shell and apt that this build (and downstream app
# builds and dev stages) depend on; the shell-less runtime variant is only for
# per-app production stages (see the hardened-images RFC).
#
# This pulls mitodl/dhi-python-mirror, NOT dhi.io directly. DHI's images carry
# zstd-compressed layers; single-platform downstream builds (nearly every app
# image built FROM this base) silently mislabel those as gzip when they push
# through Docker's legacy manifest format, which has no field to declare
# anything else — and Concourse's registry-image resource then fails trying
# to gzip-decode the real zstd bytes. The ol-python-base Concourse pipeline
# mirrors dhi.io into this repo with every layer forced to genuinely, not
# just nominally, gzip before this Dockerfile ever builds — see
# mirror_dhi_python_base_task in
# src/ol_concourse/pipelines/container_images/ol_python_base.py for the full
# mechanism and how this was confirmed.
#
# Build:
#   docker build --build-arg PYTHON_VERSION=3.12 -t mitodl/ol-python-base:3.12 .
# Multi-arch build (published for both amd64 and arm64/Apple Silicon; --push
# is required since a multi-platform result can't be loaded into the local
# docker engine):
#   docker buildx build --platform linux/amd64,linux/arm64 --push \
#     --build-arg PYTHON_VERSION=3.12 -t mitodl/ol-python-base:3.12 .
ARG PYTHON_VERSION=3.12
FROM mitodl/dhi-python-mirror:${PYTHON_VERSION}-debian13-dev

LABEL maintainer="ODL DevOps <mitx-devops@mit.edu>"

# Common-core apt packages shared across the Python services. App-specific
# extras (chromium, libcairo2-dev, operator tooling, ...) stay in each app's
# own Dockerfile / apt.txt.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      # adduser is absent from the DHI dev image's curated package set
      # (python:X-slim shipped it implicitly); required by the mitodl user
      # creation below.
      adduser \
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

# DHI's Python is Debian-packaged (`python3` in /usr/bin) rather than the
# python.org /usr/local layout that python:X-slim used, and Debian packaging
# ships no bare `python` binary. Provide one for downstream Dockerfiles and
# scripts that expect it. Guarded so this stays a no-op if a future base adds
# its own.
RUN command -v python >/dev/null 2>&1 || ln -s "$(command -v python3)" /usr/local/bin/python

# Install uv (pinned via the upstream tag; bump deliberately).
COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /uvx /usr/local/bin/

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
