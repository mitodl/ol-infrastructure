# Lifted from https://github.com/johnfmorton/docker-heroku-cli/blob/main/Dockerfile

ARG TAG=24-alpine
FROM node:$TAG

WORKDIR /app

# Install dependencies
RUN set -eux; \
    # Packages to install
    apk add --no-cache \
    && \
    apk add git \
    && \
    apk add curl \
    && \
    apk add bash \
    && \
    curl https://cli-assets.heroku.com/install.sh | sh \
    && \
    # Clean out directories that don't need to be part of the image
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Since this is a command line app, by default, we want to run the bash shell
CMD ["/bin/bash"]
