FROM  node:14-buster-slim
LABEL maintainer="MIT Open Learning (odl-devops@mit.edu)"
LABEL description="Node and Hugo, used for building OCW website"
LABEL version="0.2"
WORKDIR /tmp
ENV HUGO_VERSION='0.98.0'
ENV HUGO_NAME="hugo_extended_${HUGO_VERSION}_Linux-64bit"
ENV GO_FILE_NAME="go1.18.2.linux-amd64.tar.gz"
ENV GO_URL="https://golang.org/dl/${GO_FILE_NAME}"
ENV HUGO_URL="https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/${HUGO_NAME}.deb"
ENV BUILD_DEPS="wget"
RUN apt update && \
    apt install -y curl awscli jq git "${BUILD_DEPS}" && \
    wget "${HUGO_URL}" && \
    apt install "./${HUGO_NAME}.deb" && \
    rm -rf "./${HUGO_NAME}.deb" "${HUGO_NAME}" && \
    wget "${GO_URL}" && \
    tar -xvf ${GO_FILE_NAME} && \
    mv go /usr/local && \
    rm "${GO_FILE_NAME}" && \
    apt remove -y "${BUILD_DEPS}" && \
    apt autoremove -y && \
    apt clean && \
    rm -rf /var/lib/apt/lists/*
