# Lifted from https://github.com/johnfmorton/docker-heroku-cli/blob/main/Dockerfile

ARG TAG=26-alpine@sha256:c6ad9e9c1d80f6eb607c26b4b0a132ff7a2344fad1c78ea3baa2a7d4c8de6d94
FROM node:$TAG

# Fail the build if the Heroku installer download fails, not just the shell.
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]

WORKDIR /app

# Install dependencies
# hadolint ignore=DL3018
RUN set -eux; \
    apk add --no-cache git curl bash \
    && \
    curl https://cli-assets.heroku.com/install.sh | sh \
    && \
    # Clean out directories that don't need to be part of the image
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Since this is a command line app, by default, we want to run the bash shell
CMD ["/bin/bash"]
