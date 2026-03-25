# Original : https://raw.githubusercontent.com/taylorsilva/dcind/main/Dockerfile

FROM alpine:3

ARG EARTHLY_VERSION="v0.7.22"

# Install Docker and Docker Compose
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

# Install earthly for legacy builds of openedx
# Hard coded to the last version of earthly
RUN wget https://github.com/earthly/earthly/releases/download/v0.8.16/earthly-linux-amd64 -O /usr/local/bin/earthly && \
  chmod +x /usr/local/bin/earthly && \
  /usr/local/bin/earthly bootstrap

# Install dagger.io for newer builds of openedx and other CI tasks
RUN curl -fsSL https://dl.dagger.io/dagger/install.sh | DAGGER_VERSION=${DAGGER_VERSION} BIN_DIR=/usr/local/bin sh && \
  chmod +x /usr/local/bin/dagger

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/bin/bash"]
