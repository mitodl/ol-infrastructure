# Dockerfiles

This directory contains Dockerfiles for MIT Open Learning services.

## ol-python-base

[`ol-python-base/`](ol-python-base/) — Shared Python base image used by all MIT OL Django services.

Published as `mitodl/ol-python-base:{3.11,3.12,3.13}` via the Concourse pipeline at
[`pipelines/infrastructure/ol_python_base_docker.yaml`](../pipelines/infrastructure/ol_python_base_docker.yaml).

Bakes the substrate every app Dockerfile previously duplicated: Python slim, common-core
apt packages, the `uv` binary, non-root `mitodl` user, `/opt/venv` + `UV_CACHE_DIR`
env vars. App Dockerfiles do `FROM mitodl/ol-python-base:<python-version>` and add only
app-specific layers.

**Registering the pipeline** (one-time, requires Concourse access):
```
fly -t <target> set-pipeline \
    -p ol-python-base-docker \
    -c pipelines/infrastructure/ol_python_base_docker.yaml
```

## apisix-waf

[`apisix-waf/`](apisix-waf/) — Spike image layering the [Coraza](https://www.coraza.io/)
proxy-wasm WAF filter (OWASP Core Rule Set embedded at compile time by the
upstream `coraza-proxy-wasm` project) onto the stock `apache/apisix` image.
`wasm-nginx-module`/wasmtime are already compiled into every apisix-base
build, so this image only needs to copy the plugin binary in.

Published as `mitodl/apisix-waf` (ECR only, not Docker Hub) via the Concourse
pipeline at
[`pipelines/container_images/apisix_waf.py`](../src/ol_concourse/pipelines/container_images/apisix_waf.py).
Deliberately not chained to a Pulumi deploy -- see
`apisix_custom_image_repository`/`apisix_custom_image_tag` in
[`apisix_official.py`](../src/ol_infrastructure/infrastructure/aws/eks/apisix_official.py)
for the opt-in per-cluster config that actually references this image.

## edX / Open edX

The edX Dockerfiles that previously lived here (`openedx-edxapp`,
`openedx-codejail`, `openedx-forum`, `openedx-notes`, `openedx-xqueue`) have
been removed. All edX image builds are now managed in the
[mitodl/lehrer](https://github.com/mitodl/lehrer) repository.

The Kubernetes deployment configuration for edX applications remains in this
repository under
[`src/ol_infrastructure/applications/edxapp/`](../src/ol_infrastructure/applications/edxapp/).
