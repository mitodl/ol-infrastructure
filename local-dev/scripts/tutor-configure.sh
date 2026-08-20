#!/usr/bin/env bash
#
# Point the developer's Tutor installation at the local-dev stack.
#
# Run automatically by the openedx-tutor-config Tilt resource whenever
# openedx_mode is "tutor" (see local-dev/apps/openedx-tutor/Tiltfile), and safe
# to run by hand:
#
#     ./local-dev/scripts/tutor-configure.sh
#
# It edits the tutor root reported by `tutor config printroot` — the same one
# `tutor dev` uses for every other project on this machine. What it changes:
#
#   1. LMS_HOST / CMS_HOST / PREVIEW_LMS_HOST -> the stack's *.<root_domain>
#      hostnames, with HTTPS on and tutor's own web proxy off (APISIX is the
#      proxy, and host ports 80/443 already belong to the k3d loadbalancer).
#   2. Installs and enables the ol_local_dev tutor plugin, which rewrites the
#      dev settings' hardcoded http://host:8000 URLs to the ingress URLs.
#   3. Writes env/dev/docker-compose.override.yml so the LMS/CMS containers can
#      resolve the stack's hostnames back to the host.
#
# Nothing here is undone automatically. To go back to another Open edX setup,
# re-point the same values (see "Open edX (tutor mode)" in local-dev/README.md).
set -euo pipefail

ROOT_DOMAIN="${LOCAL_DEV_ROOT_DOMAIN:-mit.dev}"
TUTOR="${TUTOR_BIN:-tutor}"

LMS_HOST="lms.${ROOT_DOMAIN}"
CMS_HOST="studio.${ROOT_DOMAIN}"
PREVIEW_LMS_HOST="preview.lms.${ROOT_DOMAIN}"
# tutor-mfe defaults MFE_HOST to apps.<LMS_HOST>, but only for a config that
# never set it; set it explicitly so an existing value is repointed too.
MFE_HOST="apps.lms.${ROOT_DOMAIN}"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
PLUGIN_SRC="${SCRIPT_DIR}/../apps/openedx-tutor/tutor/ol_local_dev.py"

log()  { echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
err()  { echo "  ✗ $*" >&2; exit 1; }

# Runs it rather than just looking for it: a wrapper on PATH can resolve fine
# and still fail on every call (a `uv run tutor` shim outside its own project
# recurses until uv gives up), and finding that out halfway through a script is
# worse than finding it out here.
"${TUTOR}" --version &>/dev/null || err "\
'${TUTOR}' is not a working tutor.
  openedx_mode is set to 'tutor', which requires Tutor to be installed locally:
      uv tool install \"tutor[full]\"
  If it is installed but this is the wrong entry point — a wrapper script, or a
  copy in a virtualenv — name the real one:
      export TUTOR_BIN=/path/to/venv/bin/tutor
  Or set \"openedx_mode\": \"qa\" in tilt_config.json to run without Open edX.
  What went wrong when it ran:
$("${TUTOR}" --version 2>&1 | sed 's/^/      /')"

log "Configuring Tutor for the local-dev stack ($("${TUTOR}" --version))"

TUTOR_ROOT_DIR=$("${TUTOR}" config printroot)
PLUGINS_ROOT=$("${TUTOR}" plugins printroot)
ok "tutor root: ${TUTOR_ROOT_DIR}"

# Warn before repointing a tutor root that was serving some other platform:
# course data and MySQL rows survive, but every URL the LMS emits changes.
CURRENT_LMS_HOST=$("${TUTOR}" config printvalue LMS_HOST 2>/dev/null || echo "")
# www.myopenedx.com is tutor's own default, i.e. a root nobody has configured yet.
if [[ -n "${CURRENT_LMS_HOST}" \
    && "${CURRENT_LMS_HOST}" != "${LMS_HOST}" \
    && "${CURRENT_LMS_HOST}" != "www.myopenedx.com" ]]; then
    warn "LMS_HOST is currently '${CURRENT_LMS_HOST}' — repointing it to '${LMS_HOST}'."
    warn "This tutor root is shared with any other project using it."
fi

# ---------------------------------------------------------------------------
# 1. Plugin
# ---------------------------------------------------------------------------
# Symlinked rather than copied so edits to the tracked file take effect on the
# next `tutor config save` without reinstalling.
mkdir -p "${PLUGINS_ROOT}"
ln -sfn "$(cd -- "$(dirname -- "${PLUGIN_SRC}")" && pwd)/$(basename -- "${PLUGIN_SRC}")" \
    "${PLUGINS_ROOT}/ol_local_dev.py"
"${TUTOR}" plugins enable ol_local_dev >/dev/null
ok "ol_local_dev plugin installed and enabled"

# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------
# ENABLE_WEB_PROXY=false stops tutor from running its own Caddy: APISIX is the
# TLS terminator here, and it already owns host ports 80/443. In dev mode the
# LMS and CMS publish 8000/8001 directly, which is what the in-cluster proxy
# (local-dev/apps/openedx-tutor/proxy.yaml) forwards to.
"${TUTOR}" config save \
    --set "LMS_HOST=${LMS_HOST}" \
    --set "CMS_HOST=${CMS_HOST}" \
    --set "PREVIEW_LMS_HOST=${PREVIEW_LMS_HOST}" \
    --set "MFE_HOST=${MFE_HOST}" \
    --set "ENABLE_HTTPS=true" \
    --set "ENABLE_WEB_PROXY=false" \
    --set "OL_LOCAL_DEV_ROOT_DOMAIN=${ROOT_DOMAIN}" \
    >/dev/null
ok "LMS https://${LMS_HOST} · Studio https://${CMS_HOST} · MFEs https://${MFE_HOST}"

# The ol-oauth2 backend Open edX logs in through comes from this package, so it
# has to be in the image. --append is a no-op when it is already listed; adding
# it to a config that lacks it means the next `tutor dev launch` rebuilds
# openedx-dev.
"${TUTOR}" config save --append "OPENEDX_EXTRA_PIP_REQUIREMENTS=ol-social-auth" >/dev/null
ok "ol-social-auth in OPENEDX_EXTRA_PIP_REQUIREMENTS"

# ---------------------------------------------------------------------------
# 3. Compose override
# ---------------------------------------------------------------------------
# The stack's hostnames resolve to 127.0.0.1 in the developer's /etc/hosts,
# which inside a tutor container is the container itself. host-gateway points
# them back at the host, where the k3d loadbalancer is listening on 443.
#
# Note: Open edX does not yet trust the mkcert CA, so server-to-server HTTPS
# calls from the LMS into the stack still fail. Nothing in tutor mode makes
# such a call today (traffic runs mitxonline -> LMS); see the README before
# adding one.
OVERRIDE_DIR="${TUTOR_ROOT_DIR}/env/dev"
OVERRIDE_FILE="${OVERRIDE_DIR}/docker-compose.override.yml"
MARKER="# managed by ol-infrastructure local-dev (tutor-configure.sh)"

if [[ -f "${OVERRIDE_FILE}" ]] && ! grep -qF "${MARKER}" "${OVERRIDE_FILE}"; then
    mv "${OVERRIDE_FILE}" "${OVERRIDE_FILE}.bak"
    warn "existing ${OVERRIDE_FILE} moved to .bak"
fi

mkdir -p "${OVERRIDE_DIR}"
{
    echo "${MARKER}"
    echo "# Regenerated on every run; edit tutor-configure.sh instead."
    echo "x-ol-local-dev-hosts: &ol-local-dev-hosts"
    for host in \
        "mitxonline.${ROOT_DOMAIN}" \
        "mitxonline-internal.${ROOT_DOMAIN}" \
        "learn.${ROOT_DOMAIN}" \
        "api.learn.${ROOT_DOMAIN}" \
        "ai.learn.${ROOT_DOMAIN}" \
        "sso.ol.${ROOT_DOMAIN}"
    do
        echo "  - \"${host}:host-gateway\""
    done
    echo "services:"
    echo "  lms:"
    echo "    extra_hosts: *ol-local-dev-hosts"
    echo "  cms:"
    echo "    extra_hosts: *ol-local-dev-hosts"
} > "${OVERRIDE_FILE}"
ok "wrote ${OVERRIDE_FILE}"

log "Tutor configured."
