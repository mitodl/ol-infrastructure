# Lifted from https://github.com/johnfmorton/docker-heroku-cli/blob/main/Dockerfile

ARG TAG=24-alpine
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
