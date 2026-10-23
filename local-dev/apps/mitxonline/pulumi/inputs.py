"""What the local mitxonline program reads before it builds bindings.

Kept apart from ``__main__.py`` so the three decisions a teammate can trip on
(which image runs, how the stack file's ``vars`` merge, what Pulumi waits on
and how the Job fails) are plain functions with tests.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import yaml
from pulumi import ResourceTransformArgs, ResourceTransformResult

IMAGE_REPOSITORY = "mitodl/mitxonline-app"
ROOT_DOMAIN_DEFAULT = "mit.test"
# Pulumi waits for a Deployment's pods and a Service's endpoints, which is pod
# readiness either way. Tilt tracks the rollout itself and times the apply
# command out, so that wait only delays the YAML Tilt needs. The pre-deploy
# Job is not here on purpose: the webapp Deployment depends on it, so
# migrations still finish before the webapp rolls.
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
# The composed Open edX authenticates to mitxonline with the OAuth2 Application
# that mitxonline's openedx/migrations/0001_add_oauth_application creates, but
# only on a fresh database, with a random secret that django-oauth-toolkit
# hashes on save and cannot read back. Converging it with the LMS's
# OL_OAUTH2_CLIENT_{ID,SECRET} therefore means re-asserting the row from
# OPENEDX_OAUTH_APP_CLIENT_{ID,SECRET} in the Job's env whenever the Job runs.
# `configure_instance`, the documented way, exits before its OAuth step without
# a platform argument and builds redirect URIs from devstack hosts. Appended to
# the deployed migrate command, in the same shell, so it runs after the tables
# exist and a failure fails the Job. Quoted heredoc: the shell leaves the Python
# alone.
# The Wagtail home page, Site row and index pages a fresh database lacks. Every
# step is an ensure_* call, so it is safe on every Job run; the deployed stacks'
# databases never start empty, so the deployed pre-deploy command omits it.
CONFIGURE_WAGTAIL_STEP = " && python manage.py configure_wagtail"
OPENEDX_OAUTH_APP_STEP = """ && python manage.py shell <<'PY'
import os
from urllib.parse import urljoin

from django.conf import settings
from oauth2_provider.models import get_application_model

redirect_uri = urljoin(
    settings.OPENEDX_BASE_REDIRECT_URL,
    f"/auth/complete/{settings.OPENEDX_OAUTH_PROVIDER}/",
)
_, created = get_application_model().objects.update_or_create(
    name=settings.OPENEDX_OAUTH_APP_NAME,
    defaults={
        "client_id": os.environ["OPENEDX_OAUTH_APP_CLIENT_ID"],
        "client_secret": os.environ["OPENEDX_OAUTH_APP_CLIENT_SECRET"],
        "client_type": "confidential",
        "authorization_grant_type": "authorization-code",
        "skip_authorization": True,
        "redirect_uris": redirect_uri,
        "user": None,
    },
)
verb = "created" if created else "updated"
print(f"{verb} {settings.OPENEDX_OAUTH_APP_NAME} -> {redirect_uri}")
PY"""


def with_local_steps(
    pre_deploy_commands: list[tuple[str, list[str]]] | None,
) -> list[tuple[str, list[str]]]:
    """Append CONFIGURE_WAGTAIL_STEP and OPENEDX_OAUTH_APP_STEP to the first deployed
    pre-deploy command, in that order and in the same shell, so they run after
    migrate and never beside it.

    The component's field is optional; with no command to append to the steps
    would silently never run, so that is an error here.
    """
    if not pre_deploy_commands:
        msg = "the deployed definition has no pre_deploy_commands to append to"
        raise ValueError(msg)
    (name, command), *rest = pre_deploy_commands
    local_steps = CONFIGURE_WAGTAIL_STEP + OPENEDX_OAUTH_APP_STEP
    return [(name, [*command[:-1], command[-1] + local_steps]), *rest]


def image_reference(environ: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(repository, tag)`` for the webapp image.

    ``TILT_IMAGE_0`` is the image Tilt built and pushed to the k3d registry,
    always tagged. Without a checkout Tilt builds nothing and the published
    image runs, at the tag the developer pinned with ``prebuilt_tags``
    (``LOCAL_DEV_PREBUILT_IMAGE_TAGS``, set by the root Tiltfile) or ``latest``.
    """
    built = environ.get("TILT_IMAGE_0")
    if built:
        repository, _, tag = built.rpartition(":")
        return repository, tag
    pinned = json.loads(environ.get("LOCAL_DEV_PREBUILT_IMAGE_TAGS") or "{}")
    return IMAGE_REPOSITORY, pinned.get(IMAGE_REPOSITORY, "latest")


def manifest(path: Path, root_domain: str) -> dict[str, Any]:
    """Load a manifest with ROOT_DOMAIN_DEFAULT swapped for root_domain."""
    return yaml.safe_load(path.read_text().replace(ROOT_DOMAIN_DEFAULT, root_domain))


def merged_env(
    defaults: Mapping[str, str],
    overrides: Mapping[str, Any] | None,
    root_domain: str,
) -> tuple[dict[str, str], list[str]]:
    """Return the app's environment and the keys the developer overrode.

    ``defaults`` (the program's ``LOCAL_ENV_DEFAULTS``) with ``overrides``, the
    stack file's ``mitxonline:vars`` (the deployed stacks' key for the same
    thing), merged over it. The result goes into each container's ``env:``,
    which Kubernetes gives precedence over every ``envFrom`` source, so an
    override also beats the tracked Secret. ROOT_DOMAIN_DEFAULT in a value is
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
        msg = f"mitxonline:vars values must be strings (quote them): {non_strings}"
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
