# DL3008: Debian package versions are not pinned here, matching
#   dockerfiles/ol-python-base.  This is a throwaway test image, and pinning
#   would break the build every time Debian rolls a patch release.
# DL3002/DL3066: the image ends as root on purpose -- the test-nginx harness
#   writes t/servroot and binds a listener.  Nothing is published from it.
# hadolint global ignore=DL3008,DL3002,DL3066
#
# Runs APISIX's own test-nginx harness against the Lua in this repo.
#
# Built on the same image the cluster runs (see APISIX_CHART in
# bridge/lib/versions.py -- chart 2.16.x ships APISIX 3.17.x), so the Lua is
# exercised by the same OpenResty build, with APISIX's real lua_package_path.
#
# t/APISIX.pm and the certs it reads are fetched at build time rather than
# vendored: they are upstream test fixtures, and the key files would trip
# secret scanning if committed here.
ARG APISIX_VERSION=3.17.0
FROM apache/apisix:${APISIX_VERSION}-debian

# Repeated with its default: a global ARG's default is not inherited by a
# stage-level redeclaration, so omitting it here silently yields an empty string
# and the fetch below 404s.
ARG APISIX_VERSION=3.17.0
USER root

# The image ships a minimal perl with no prove and no Test::Harness, so the
# harness and its toolchain have to be installed outright.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        perl \
        cpanminus \
        make \
        gcc \
        libc6-dev \
        libssl-dev \
        curl \
        ca-certificates \
    && cpanm --notest --quiet Test::Nginx IPC::Run3 \
    && apt-get purge -y --auto-remove gcc make libc6-dev \
    && rm -rf /var/lib/apt/lists/* /root/.cpanm

WORKDIR /usr/local/apisix

# APISIX.pm and the cert fixtures it opens, taken from the source tarball for
# the version this image runs.
#
# One request for the whole tarball, not one per file: fetching the nine files
# individually from raw.githubusercontent.com reliably trips its rate limiter
# (HTTP 429) and turned this build into a coin flip.  --retry rides out the
# remaining transient failures so a rate limit does not read as a test failure.
RUN set -eu; \
    curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors \
        -o /tmp/apisix-src.tar.gz \
        "https://github.com/apache/apisix/archive/refs/tags/${APISIX_VERSION}.tar.gz"; \
    tar -xzf /tmp/apisix-src.tar.gz -C /tmp; \
    src="/tmp/apisix-${APISIX_VERSION}"; \
    mkdir -p t/certs; \
    cp "${src}/t/APISIX.pm" t/APISIX.pm; \
    for cert in apisix.crt apisix.key apisix_ecc.crt apisix_ecc.key \
                test2.crt test2.key etcd.pem etcd.key; do \
        cp "${src}/t/certs/${cert}" "t/certs/${cert}"; \
    done; \
    rm -rf /tmp/apisix-src.tar.gz "${src}"

# etcd, because APISIX.pm boots APISIX's real init path -- which reaches for
# etcd whatever the test asserts.  Without it every test fails test-nginx's
# default "no [error] in error.log" check on connection-refused noise.  Giving
# the harness a real etcd is the honest fix; filtering those lines out would
# also hide errors we do want to see.
ARG ETCD_VERSION=3.5.21
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors \
        -o /tmp/etcd.tar.gz \
        "https://github.com/etcd-io/etcd/releases/download/v${ETCD_VERSION}/etcd-v${ETCD_VERSION}-linux-${arch}.tar.gz"; \
    tar -xzf /tmp/etcd.tar.gz -C /tmp; \
    mv "/tmp/etcd-v${ETCD_VERSION}-linux-${arch}/etcd" /usr/local/bin/etcd; \
    rm -rf /tmp/etcd.tar.gz "/tmp/etcd-v${ETCD_VERSION}-linux-${arch}"; \
    etcd --version

# The Lua under test, taken from the one place it lives.  Build context is the
# repository root (see run.sh) precisely so this can reference src/ directly --
# copying the file into this directory instead would leave a second copy free to
# drift, and the suite would then pass against Lua the gateway never runs.
COPY src/ol_infrastructure/components/services/files/oidc_error_callback_recovery.lua \
     apisix/plugins/ol/
COPY tests/apisix_testnginx/oidc_error_callback_recovery.t t/
COPY tests/apisix_testnginx/run-tests.sh /usr/local/bin/run-tests.sh
RUN chmod +x /usr/local/bin/run-tests.sh

ENV APISIX_HOME=/usr/local/apisix
ENV TEST_NGINX_BINARY=/usr/local/openresty/bin/openresty
ENV PERL5LIB=/usr/local/apisix

ENTRYPOINT ["/usr/local/bin/run-tests.sh"]
