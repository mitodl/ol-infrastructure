# Original : https://raw.githubusercontent.com/taylorsilva/dcind/main/Dockerfile

FROM alpine:3

# Install Docker and Docker Compose
# hadolint ignore=DL3018
RUN apk --no-cache add \
    bash \
    cargo \
    curl \
    docker \
    docker-cli-compose \
    device-mapper \
    gcc \
    git \
    iptables \
    libc-dev \
    libffi-dev \
    make \
    openssl-dev \
    py3-pip \
    python3-dev \
    rust \
    util-linux

# Include functions to start/stop docker daemon
COPY docker-lib.sh /docker-lib.sh
COPY entrypoint.sh /entrypoint.sh

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Set by the Concourse pipeline from the dagger github-release resource.
# Empty falls back to whatever install.sh calls latest.
ARG DAGGER_VERSION=""

# Install dagger.io for newer builds of openedx and other CI tasks
RUN curl -fsSL https://dl.dagger.io/dagger/install.sh | DAGGER_VERSION=${DAGGER_VERSION} BIN_DIR=/usr/local/bin sh && \
  chmod +x /usr/local/bin/dagger

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/bin/bash"]
