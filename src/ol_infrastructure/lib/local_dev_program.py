"""Helpers for the Pulumi programs under ``local-dev/apps/*/pulumi``.

Tilt runs each of those programs through ``local-dev/scripts/pulumi-app-apply.sh``
to render an application's deployed definition for the k3d cluster. Nothing
deployed imports this module. It lives in ``src/`` so the programs can import it
without a ``sys.path`` insert and ``mypy src/`` type-checks it.
"""

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pulumi
import pulumi_kubernetes as k8s
import yaml
from pulumi import ResourceTransformArgs, ResourceTransformResult

ROOT_DOMAIN_DEFAULT = "mit.test"
# The key pulumi-app-apply.sh selects the applied objects by. It joins every
# Deployment's immutable selector, so changing it replaces every Deployment on
# every developer's cluster.
MANAGED_BY_LABEL = {"ol.mit.edu/managed-by": "local-dev-pulumi"}
# Public roots plus the mkcert root, so pods trust both public endpoints and
# local TLS. The core stack writes this ConfigMap into every app namespace
# (CA_BUNDLE_CONFIGMAP in local-dev/infra/modules/tls.py).
CA_BUNDLE_CONFIGMAP = "local-dev-ca-bundle"
CA_BUNDLE_DIR = "/etc/ssl/local-dev"
CA_BUNDLE_FILE = f"{CA_BUNDLE_DIR}/ca-certificates.crt"
# Pulumi waits for a Deployment's pods and a Service's endpoints, which is pod
# readiness either way. Tilt tracks the rollout itself and times the apply
# command out, so that wait only delays the YAML Tilt needs. The pre-deploy
# Job is not here on purpose: every Deployment depends on it, so
# migrations finish before any of them rolls.
SKIP_AWAIT_TYPES = frozenset(
    {"kubernetes:apps/v1:Deployment", "kubernetes:core/v1:Service"}
)
JOB_TYPE = "kubernetes:batch/v1:Job"
# Pulumi awaits the Job until it succeeds or fails. Its awaiter honours the
# `pulumi.com/timeoutSeconds` annotation and the engine's CustomTimeouts as a
# value but re-arms the timer on every Job or Pod event (pulumi-kubernetes
# 4.34.2, provider/pkg/await/job.go: `time.After` inside the select loop), so
# a crash-looping migrate pod keeps either timeout from ever firing. Tilt's
# 300 s apply timeout (the root Tiltfile's update_settings) then kills
# `pulumi up` mid-create and the developer sees a timeout, not the migrate
# error. So the Job fails fast instead. Kubernetes' default backoffLimit of 6
# retries for about ten minutes; three attempts still cover a pod that starts
# before Postgres answers, and a real migrate error fails the apply within a
# minute with the Job's log on stderr (pulumi-app-apply.sh prints it).
JOB_BACKOFF_LIMIT = 2


def image_reference(repository: str, environ: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(repository, tag)`` for the webapp image.

    ``TILT_IMAGE_0`` is the image Tilt built and pushed to the k3d registry,
    always tagged. Without a checkout Tilt builds nothing and the published
    image ``repository`` runs, at the tag the developer pinned with
    ``prebuilt_tags`` (``LOCAL_DEV_PREBUILT_IMAGE_TAGS``, set by the root
    Tiltfile) or ``latest``.
    """
    built = environ.get("TILT_IMAGE_0")
    if built:
        built_repository, _, tag = built.rpartition(":")
        return built_repository, tag
    pinned = json.loads(environ.get("LOCAL_DEV_PREBUILT_IMAGE_TAGS") or "{}")
    return repository, pinned.get(repository, "latest")


def manifest(path: Path, root_domain: str) -> dict[str, Any]:
    """Load a manifest with ROOT_DOMAIN_DEFAULT swapped for root_domain."""
    return yaml.safe_load(path.read_text().replace(ROOT_DOMAIN_DEFAULT, root_domain))


def merged_env(
    section: str,
    defaults: Mapping[str, str],
    overrides: Mapping[str, Any] | None,
    root_domain: str,
) -> tuple[dict[str, str], list[str]]:
    """Return the app's environment and the keys the developer overrode.

    ``defaults`` (the program's ``LOCAL_ENV_DEFAULTS``) with ``overrides``, the
    stack file's ``section`` (the deployed stacks' key for the same thing, such as
    ``mitxonline:vars``), merged over it. The result goes into each container's
    ``env:``, which Kubernetes gives precedence over every ``envFrom`` source, so
    an override also beats the tracked Secret. ROOT_DOMAIN_DEFAULT in a value is
    swapped for ``root_domain``, as in every file under local-dev; a bare
    ``KEY:`` is ``""`` and any other non-string is an error, as in tiltlib.
    """
    env = dict(defaults)
    values = {
        key: ("" if value is None else value)
        for key, value in (overrides or {}).items()
    }
    non_strings = sorted(
        key for key, value in values.items() if not isinstance(value, str)
    )
    if non_strings:
        msg = f"{section} values must be strings (quote them): {non_strings}"
        raise ValueError(msg)
    env.update(
        {
            key: value.replace(ROOT_DOMAIN_DEFAULT, root_domain)
            for key, value in values.items()
        }
    )
    return env, sorted(values)


def _with_annotations(props: Mapping[str, Any], annotations: Mapping[str, str]):
    # The engine hands transforms plain dicts, but ResourceTransformArgs types
    # props as Inputs, so the metadata value is narrowed explicitly.
    metadata = dict(cast("dict[str, Any]", props.get("metadata") or {}))
    metadata["annotations"] = {**(metadata.get("annotations") or {}), **annotations}
    return {**props, "metadata": metadata}


def await_policy(
    args: ResourceTransformArgs,
    job_annotations: Mapping[str, str] = MappingProxyType({}),
) -> ResourceTransformResult | None:
    """Skip the readiness wait on Deployments and Services; make the Job fail fast.

    ``job_annotations`` land on the Job's pod template, the one place a change
    replaces the Job and so runs it again: pulumi-kubernetes replaces a Job only
    for a change under ``spec`` (provider/pkg/provider/diff.go) and patches the
    Job's own metadata in place. The component stamps its config hash on the
    Deployments' pod templates only, so the Job's hash comes from here.
    """
    if args.type_ == JOB_TYPE:
        spec = dict(cast("dict[str, Any]", args.props.get("spec") or {}))
        spec["backoffLimit"] = JOB_BACKOFF_LIMIT
        spec["template"] = _with_annotations(
            cast("dict[str, Any]", spec.get("template") or {}), job_annotations
        )
        # A failed Job stays in the state unhealthy, and an apply whose inputs did
        # not change re-reports that failure instead of running migrate again
        # (observed on 4.34.2), so a fix made outside the Job's inputs (the
        # database, say) would never be retried. replaceUnready makes the
        # provider replace the Job on the next update.
        props = _with_annotations(
            {**args.props, "spec": spec}, {"pulumi.com/replaceUnready": "true"}
        )
        return ResourceTransformResult(props=props, opts=args.opts)
    if args.type_ not in SKIP_AWAIT_TYPES:
        return None
    return ResourceTransformResult(
        props=_with_annotations(args.props, {"pulumi.com/skipAwait": "true"}),
        opts=args.opts,
    )


def k3d_provider() -> k8s.Provider:
    """Return the Kubernetes provider for the local k3d cluster.

    The kubeconfig is an explicit path, as the core and apps-infra stacks pass.
    Tilt hands the commands it runs a KUBECONFIG under ~/Library/Application
    Support/tilt-dev (the macOS path) that exists only while Tilt runs, and the
    provider records the kubeconfig it was given as a provider input: left to the
    environment, `tilt down` after Tilt has exited (its delete command runs the
    program's destroy) and teardown.sh would read a file that is gone and fail to
    reach the cluster.
    """
    kubeconfig = str(
        Path(pulumi.Config().get("kubeconfig") or "~/.kube/config").expanduser()
    )
    return k8s.Provider("k3d-local", context="local-dev", kubeconfig=kubeconfig)


def ca_bundle_volume() -> k8s.core.v1.VolumeArgs:
    """Return the volume that carries the CA_BUNDLE_CONFIGMAP."""
    return k8s.core.v1.VolumeArgs(
        name=CA_BUNDLE_CONFIGMAP,
        config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=CA_BUNDLE_CONFIGMAP),
    )


def ca_bundle_mount() -> k8s.core.v1.VolumeMountArgs:
    """Return the mount of ``ca_bundle_volume()`` at CA_BUNDLE_DIR."""
    return k8s.core.v1.VolumeMountArgs(
        name=CA_BUNDLE_CONFIGMAP, mount_path=CA_BUNDLE_DIR, read_only=True
    )


def ca_bundle_env() -> dict[str, str]:
    """Return the variables that point Python's TLS stacks at the mounted bundle."""
    return {"REQUESTS_CA_BUNDLE": CA_BUNDLE_FILE, "SSL_CERT_FILE": CA_BUNDLE_FILE}


def warn_leftover_app_env_local(app_dir: Path, section: str) -> None:
    """Name the keys of a ``configmaps/app-env.local.yaml`` that nothing reads.

    The YAML-applied apps take overrides from that file; a Pulumi-rendered app
    takes them from ``section`` in its stack file, so for such an app the file is
    inert. Names only: the values may be secrets. Delete this check once no app
    reads app-env.local.yaml.
    """
    leftover = app_dir / "configmaps/app-env.local.yaml"
    if leftover.exists():
        keys = ", ".join((yaml.safe_load(leftover.read_text()) or {}).get("data") or {})
        sys.stderr.write(
            f"configmaps/app-env.local.yaml is not read: move {keys or 'its data:'} "
            f"under {section} in pulumi/Pulumi.Dev.yaml, then delete it\n"
        )


def secrets_hash(data: Mapping[str, str]) -> str:
    """Return a digest of the Secret's data, for the workloads' config hash."""
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
