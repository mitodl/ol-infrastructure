# syntax=docker/dockerfile:1
# Keycloak image for testing an unreleased ol-keycloakify theme in the local-dev
# stack. local-dev/scripts/kc-theme-image.sh drives this build; usage is under
# "Testing a local ol-keycloakify build" in local-dev/README.md. The build
# context is the ol-keycloakify checkout, not this repo, and the theme jar is
# built from that checkout's sources here, so the host needs neither Node nor
# Maven and a stale dist_keycloak/ cannot leak into the image.
#
# The base is the published image the core stack runs by default, so the only
# difference from a stock local-dev deployment is the theme jar. The operator
# starts Keycloak with --optimized, and an optimized Keycloak refuses to boot
# when a jar in providers/ no longer matches the one it was built against, so
# a new theme cannot be mounted or copied into a running pod: it has to be
# baked in and `kc.sh build` rerun. BASE_IMAGE has no default on purpose: the
# script reads the digest the core stack pins in
# local-dev/infra/core/__main__.py and passes it in, so the two cannot drift.
ARG BASE_IMAGE

# The jar is platform-independent, so build it on the host's own architecture.
# Only the Keycloak stage below has to be linux/amd64 (the base is amd64-only).
FROM --platform=$BUILDPLATFORM node:24-bookworm AS theme
# keycloakify packages the theme with Maven; Debian's package brings a JDK.
# Any Maven 3 works for that, so the exact Debian package version is not
# worth pinning (and pins rot as bookworm point releases roll).
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends maven \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /theme
# Dependencies first so a source-only change reuses the installed node_modules
# layer. The checkout vendors its own Yarn under .yarn/releases (yarnPath in
# .yarnrc.yml), so the image's stock yarn just hands off to it.
COPY package.json yarn.lock .yarnrc.yml ./
COPY .yarn/ .yarn/
RUN --mount=type=cache,target=/root/.yarn/berry/cache \
    yarn install --immutable --mode=skip-build
# The project's postinstall (keycloakify sync-extensions) was skipped above
# because it writes generated account-theme sources into src/, which is not
# copied yet. Run it now, then build.
COPY . .
RUN --mount=type=cache,target=/root/.m2 \
    yarn run postinstall \
    && yarn build-keycloak-theme

FROM ${BASE_IMAGE}
# Remove the released theme first: keycloakify has shipped its jar under more
# than one filename, and two jars declaring the same theme names is an error.
RUN rm -f /opt/keycloak/providers/keycloakify-theme*.jar \
    /opt/keycloak/providers/keycloak-theme-for-kc-*.jar
# The jar name is fixed by keycloakifyBuildOptions in the checkout's vite.config.ts.
COPY --from=theme /theme/dist_keycloak/keycloakify-theme_v11-21_and_v26plus.jar \
    /opt/keycloak/providers/

# Same flags as Dockerfile.hosted in mitodl/ol-keycloak, which built the base.
# A build-time option that differs from the base would make the pod crash at
# start because the CR's runtime options assume the hosted build.
RUN /opt/keycloak/bin/kc.sh build \
    --db=postgres \
    --health-enabled=true \
    --metrics-enabled=true \
    --event-metrics-user-enabled=true \
    --spi-login--provider=ol-freemarker \
    --tracing-enabled=true \
    --tracing-jdbc-enabled=true \
    --features=update-email
