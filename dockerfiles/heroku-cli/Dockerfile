# Lifted from https://github.com/johnfmorton/docker-heroku-cli/blob/main/Dockerfile

ARG TAG=26-alpine@sha256:8996bb5821d62a8fd9bb27c1cb07462f3fdce6e47d640c6a881ead0b064ce216
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
