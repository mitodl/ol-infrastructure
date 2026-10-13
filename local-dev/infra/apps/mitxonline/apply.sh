#!/usr/bin/env bash
# Tilt's k8s_custom_deploy commands for the mitxonline local stack.
#
#   apply.sh up      pulumi up, then print the applied objects as YAML
#   apply.sh down    pulumi destroy
#
# Tilt reads stdout as the applied objects, so everything Pulumi prints goes to
# stderr. TILT_IMAGE_0 (the image Tilt built) reaches the program through the
# environment.
set -euo pipefail

cd "$(dirname "$0")"
STACK=local-dev.mitxonline.Dev
export PULUMI_CONFIG_PASSPHRASE=""
repo_root="$(cd ../../../.. && pwd)"

pulumi_() { uv run --project "$repo_root" pulumi "$@" --stack "$STACK" 1>&2; }

case "${1:-}" in
up)
  now() { python3 -c "import time; print(time.time())"; }
  start=$(now)
  uv run --project "$repo_root" pulumi stack select --create "$STACK" 1>&2
  pulumi_ up --yes --skip-preview
  end=$(now)
  echo "{\"pulumi_up_s\": $(python3 -c "print(round($end - $start, 3))"), \"image\": \"${TILT_IMAGE_0:-}\", \"at\": \"$(date -u +%FT%TZ)\"}" \
    >>"$repo_root/local-dev/infra/.pulumi/pilot-timings.jsonl"
  kubectl --context local-dev -n mitxonline get deploy,svc \
    -l ol.mit.edu/managed-by=local-dev-pulumi -o yaml
  ;;
down)
  pulumi_ destroy --yes --skip-preview
  # The program skips Pulumi's await on Deployments and Services, which skips
  # the wait for deletion too. Tilt runs `up` straight after `down` on a force
  # update, and creating over a still-terminating object fails.
  kubectl --context local-dev -n mitxonline wait --for=delete deploy,svc,job,pod \
    -l ol.mit.edu/managed-by=local-dev-pulumi --timeout=180s 1>&2 || true
  ;;
*)
  echo "usage: $0 up|down" >&2
  exit 2
  ;;
esac
