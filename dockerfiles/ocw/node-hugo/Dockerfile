FROM node:22-trixie-slim
LABEL maintainer="MIT Open Learning (odl-devops@mit.edu)"
LABEL description="Node and Hugo, used for building OCW website"
LABEL version="0.8"
WORKDIR /tmp

# renovate: datasource=github-releases depName=hugo packageName=gohugoio/hugo
ENV HUGO_VERSION="0.152.2"
ENV HUGO_NAME="hugo_extended_${HUGO_VERSION}_linux-amd64"

# renovate: datasource=github-tags depName=go packageName=golang/go versioning=go
ENV GO_VERSION="1.23.0"
ENV GO_FILE_NAME="go${GO_VERSION}.linux-amd64.tar.gz"
ENV GO_URL="https://golang.org/dl/${GO_FILE_NAME}"
ENV HUGO_URL="https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/${HUGO_NAME}.deb"

RUN apt-get update && \
    apt-get install --no-install-recommends -y curl awscli jq git wget zip unzip && \
    wget --progress=dot:giga "${HUGO_URL}" && \
    apt-get install -y "./${HUGO_NAME}.deb" && \
    rm -rf "./${HUGO_NAME}.deb" "${HUGO_NAME}" && \
    wget --progress=dot:giga "${GO_URL}" && \
    tar -xvf ${GO_FILE_NAME} && \
    mv go /usr/local && \
    rm "${GO_FILE_NAME}" && \
    apt-get remove -y "${BUILD_DEPS}" && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
