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

RUN wget https://github.com/earthly/earthly/releases/download/${EARTHLY_VERSION}/earthly-linux-amd64 -O /usr/local/bin/earthly && \
  chmod +x /usr/local/bin/earthly && \
  /usr/local/bin/earthly bootstrap

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/bin/bash"]
