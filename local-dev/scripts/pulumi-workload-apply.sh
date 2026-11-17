#!/usr/bin/env bash
# Tilt's apply command for one Deployment of a Pulumi-rendered app that has a
# Tilt resource of its own (pulumi_app_workloads in local-dev/tiltlib.star).
#
#   pulumi-workload-apply.sh <app> <deployment> [<container>]
#
# Tilt reads stdout as the applied object, so the Deployment is printed last
# and everything else goes to stderr. Given a <container> and an image Tilt
# built for this resource, the Deployment is first pointed at the newest build
# of that image (tilt-newest-image.sh). That leaves kubectl owning the image
# field, which does not make the app's next `pulumi up` conflict: before it
# applies, pulumi-kubernetes takes over the fields a `kubectl*` manager set
# (its fixCSAFieldManagers).
set -euo pipefail
usage="usage: $0 <app> <deployment> [<container>]"
app="${1:?$usage}"
deployment="${2:?$usage}"
kubectl_() { kubectl --context local-dev -n "$app" "$@"; }

image="$("$(dirname "$0")/tilt-newest-image.sh")"
if [ -n "${3:-}" ] && [ -n "$image" ]; then
    kubectl_ set image "deploy/$deployment" "$3=$image" 1>&2
    # Keeps the resource "updating" through a normal roll. A pod that never
    # becomes ready shows on the resource once the Deployment is printed, so
    # a rollout still unfinished here is not an error.
    kubectl_ rollout status "deploy/$deployment" --timeout=60s 1>&2 ||
        echo "$deployment has not finished rolling out; see its pod" >&2
fi
kubectl_ get deploy "$deployment" -o yaml
