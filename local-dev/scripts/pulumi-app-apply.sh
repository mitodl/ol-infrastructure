#!/usr/bin/env bash
# Tilt's k8s_custom_deploy commands for an app rendered by a local Pulumi
# program under local-dev/apps/<app>/pulumi (stack `Dev`, the project-scoped
# short name the deployed stacks use, so the stack file is Pulumi.Dev.yaml; the
# app's own namespace; the file backend under local-dev/infra/.pulumi).
#
#   pulumi-app-apply.sh <app> up      pulumi up, then print the objects it owns as YAML
#   pulumi-app-apply.sh <app> down    pulumi destroy, then wait for those objects to go
#
# Tilt reads stdout as the applied objects, so everything Pulumi says goes to
# stderr. The program reads its inputs (the image Tilt built, the root domain,
# the prebuilt-tag pin) from the environment Tilt passes, and the developer's
# env and memory limits from the gitignored stack file `stack select --create`
# writes next to it on the first run.
set -euo pipefail
app="${1:?usage: $0 <app> up|down}"
repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
program_dir="$repo_root/local-dev/apps/$app/pulumi"
project="$(sed -n 's/^name: *//p' "$program_dir/Pulumi.yaml")"
STACK="Dev"
export PULUMI_CONFIG_PASSPHRASE=""
# uv honours FORCE_COLOR/CLICOLOR_FORCE even when piped, and Pulumi's Python
# host parses `uv export` stdout as TOML, so a colour-forcing environment fails
# `pulumi up` ("TOML files cannot contain control characters"). Tilt sets
# FORCE_COLOR=1 for local_resource commands; the root Tiltfile unsets it for
# the core and apps-infra stacks the same way.
unset FORCE_COLOR CLICOLOR_FORCE
lock_dir="$repo_root/local-dev/infra/.pulumi/.pulumi/locks/organization/$project/$STACK"
selector="ol.mit.edu/managed-by=local-dev-pulumi"
cd "$program_dir"

pulumi_() { uv run --project "$repo_root" pulumi "$@" --stack "$STACK" 1>&2; }
kubectl_() { kubectl --context local-dev -n "$app" "$@"; }

# A Tilt timeout or a killed run leaves the lock behind.
"$repo_root/local-dev/scripts/pulumi-clear-stale-lock.sh" "$lock_dir"
# A lock the helper left is a live run (Tilt's, or a copy of this script run by
# hand); the pending-create cleanup below must not delete its objects.
if compgen -G "$lock_dir/*.json" >/dev/null; then
    echo "another pulumi run holds the $project/$STACK lock; wait for it to finish" >&2
    exit 1
fi
uv run --project "$repo_root" pulumi stack select --create "$STACK" 1>&2
# The same kill, landing between an object's creation and the end of its await
# (the Job's await lasts as long as migrate), leaves a pending create in the
# checkpoint and an object the state doesn't know: up then fails with "already
# exists" and destroy leaves it behind. Delete those objects and drop the
# entries; up recreates them (the Job's migrate is idempotent).
pending="$(uv run --project "$repo_root" pulumi stack export --stack "$STACK" | "$repo_root/local-dev/scripts/pulumi-pending-objects.py")"
if [ -n "$pending" ]; then
    echo "clearing operations a killed run left mid-create: $pending" >&2
    # shellcheck disable=SC2086
    kubectl_ delete $pending --ignore-not-found 1>&2
    pulumi_ refresh --yes --skip-preview --clear-pending-creates
fi

case "${2:-}" in
up)
    # A Deployment, Service, Secret, ConfigMap or route Tilt applied as YAML
    # (label managed-by=tilt) has a rendered object's name or shadows its routes,
    # and Pulumi adopts nothing. An ApisixTls Tilt applies on purpose is not
    # listed.
    legacy="deploy,svc,secret,cm,apisixroute,apisixpluginconfig -l app.kubernetes.io/managed-by=tilt"
    # shellcheck disable=SC2086
    if leftovers="$(kubectl_ get $legacy -o name)" && [ -n "$leftovers" ]; then
        echo "$app objects Tilt applied as YAML are still in the cluster:" >&2
        echo "$leftovers" >&2
        echo "remove them first: kubectl --context local-dev -n $app delete $legacy" >&2
        exit 1
    fi
    # --refresh: state lives in the checkout and outlives the cluster, as for
    # the core and apps-infra stacks.
    if ! pulumi_ up --yes --skip-preview --refresh; then
        # The component labels the pre-deploy Job's pods ol.mit.edu/job=pre-deploy.
        kubectl_ logs -l "$selector,ol.mit.edu/job=pre-deploy" --all-containers --tail=100 1>&2 || :
        exit 1
    fi
    kubectl_ get deploy,svc,apisixroute,apisixpluginconfig -l "$selector" -o yaml
    ;;
down)
    pulumi_ destroy --yes --skip-preview
    # The program skips Pulumi's await on Deployments and Services, which skips
    # the wait for deletion too. A `tilt trigger` is a force update: Tilt runs
    # this and then `up` straight away, so `up` must not race the deletes.
    kubectl_ wait --for=delete deploy,svc,job,pod -l "$selector" --timeout=180s 1>&2
    ;;
*)
    echo "usage: $0 <app> up|down" >&2
    exit 2
    ;;
esac
