# Spike: layers the Coraza WAF proxy-wasm filter (OWASP Core Rule Set embedded
# at compile time by the upstream project -- no separate ruleset files needed)
# onto the stock APISIX image. wasm-nginx-module/wasmtime are already compiled
# into every apisix-base build (confirmed against api7/apisix-build-tools'
# build-apisix-base.sh), so this needs nothing beyond copying the plugin binary
# in and registering it via Helm values (see apisix_official.py).
#
# Base tag must track APISIX_CHART_VERSION's default appVersion
# (src/bridge/lib/versions.py) -- chart 2.16.0 => APISIX 3.17.0-ubuntu.
ARG APISIX_IMAGE_TAG="3.17.0-ubuntu"
ARG CORAZA_PROXY_WASM_VERSION="0.6.0"

FROM ghcr.io/corazawaf/coraza-proxy-wasm:${CORAZA_PROXY_WASM_VERSION} AS coraza

FROM apache/apisix:${APISIX_IMAGE_TAG}

COPY --from=coraza /plugin.wasm /usr/local/apisix/coraza-filter.wasm
