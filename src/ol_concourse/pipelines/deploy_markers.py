"""The cross-pipeline contract for "a deploy to this environment finished".

Concourse's `passed` constraint is pipeline-local: a job in ``canary-mit-learn``
cannot be gated on a job in ``mit-learn-pipeline``, and there is no
trigger-another-pipeline primitive. The only way one pipeline observes another's
progress is a resource both can see. This module is that resource.

A *deploy marker* is a small JSON object written to S3 by the job that finished
the deploy, under a key whose final path segment is the release version::

    s3://ol-eng-artifacts/deploy-markers/<app>/<environment>/<version>.json

The version is in the **key**, not just the body, because Concourse's ``s3``
resource extracts its version from the key via ``regexp``. That makes the release
version the resource version -- so a canary build triggered by a deploy names, on
its own build page and in its own inputs, exactly which release it tested. A
marker whose version lived only in the body would trigger builds that cannot be
attributed to anything.

Producer and consumer both build the resource from :func:`deploy_marker_resource`
rather than each spelling the layout out. That is the point of this module: a
marker written under a key the consumer's ``regexp`` does not match is a canary
that silently stops deploy-triggering, with nothing red anywhere to say so. One
function means the two cannot drift.

Current participants:

- Producer: ``ol_concourse.pipelines.infrastructure.k8s_apps.pipeline``, opt-in
  per app via ``AppPipelineParams.publish_rc_deploy_marker``.
- Consumer: ``ol_concourse.pipelines.canaries.pipeline``, opt-in per property via
  ``CanaryParams.deploy_trigger``.

Both meta pipelines watch this file, so a change to the layout re-renders both
sides together. If you add a third participant, add its meta pipeline's watch
list too.
"""

import json

from ol_concourse.lib.models.pipeline import Identifier, Resource

# The operations Concourse workers' instance role already carries
# Get/Put/List on this bucket (see
# ol_infrastructure/applications/concourse/iam_policies/operations.py), which is
# why the resource below configures no credentials and needs no Vault entry.
DEPLOY_MARKER_BUCKET = "ol-eng-artifacts"
DEPLOY_MARKER_PREFIX = "deploy-markers"

# The environment name used in marker keys for the release-candidate
# environment.
#
# Note the deliberate mismatch with the Pulumi stack that deploys it, which is
# named ``QA``. Everything user-facing calls this environment RC -- the hostname
# is rc.learn.mit.edu, the GitHub Deployment environment the modernized pipeline
# shape creates is literally "RC", and the runbooks say "deployed to RC". Naming
# the marker after the stack would mean a canary author reading
# ``deploy-markers/mit-learn/QA/`` had to know that QA and RC are the same thing.
# So: markers use RC, and the k8s_apps producer maps its stack index 0 (``QA``)
# onto it at the one place that emits them.
RC_ENVIRONMENT = "RC"

# The version the resource reports before any real marker exists.
#
# Without this the very first ``get`` has no version to resolve and the consuming
# job never becomes schedulable -- which for a canary means the *scheduled* runs
# stop too, because both triggers feed one job. A synthetic initial version keeps
# the canary running from the moment it is set, and reports an obviously fake
# release until the first deploy overwrites it. It sorts below every real calver
# (2026.9.21.1) under the s3 resource's semantic ordering.
# The S104 suppression below is a false positive: this is a version string, not
# a bind address.
INITIAL_MARKER_VERSION = "0.0.0.0"  # noqa: S104


def deploy_marker_directory(app_name: str, environment: str) -> str:
    """Return the bucket-relative directory markers for this app+env live under.

    :param app_name: Application name as it appears in ``k8s_apps`` (e.g.
        ``mit-learn``).
    :param environment: Environment name, e.g. :data:`RC_ENVIRONMENT`.
    :returns: A key prefix with no leading or trailing slash.
    """
    return f"{DEPLOY_MARKER_PREFIX}/{app_name}/{environment}"


def deploy_marker_regexp(app_name: str, environment: str) -> str:
    """Return the ``s3`` resource ``regexp`` matching this app+env's markers.

    The single capture group is the release version, which becomes the Concourse
    resource version. Restricted to digits and dots rather than ``(.*)`` so a
    stray object dropped in this prefix cannot become a "release" that triggers a
    canary.

    :param app_name: Application name.
    :param environment: Environment name.
    :returns: A regular expression anchored on both ends by the s3 resource.
    """
    return rf"{deploy_marker_directory(app_name, environment)}/([0-9.]+)\.json"


def deploy_marker_filename(version: str) -> str:
    """Return the object basename carrying ``version``.

    :param version: Release version, e.g. ``2026.9.21.1``.
    :returns: The file name a producer must write for the regexp to match.
    """
    return f"{version}.json"


def deploy_marker_resource(
    name: Identifier, app_name: str, environment: str
) -> Resource:
    """Build the shared S3 resource for one app+environment's deploy markers.

    Used unchanged by both ends -- the producing pipeline ``put``s to it and the
    consuming pipeline ``get``s from it. Keep it that way: a producer-only and a
    consumer-only variant is exactly the drift this module exists to prevent.

    :param name: Resource name within the calling pipeline.
    :param app_name: Application name as it appears in ``k8s_apps``.
    :param environment: Environment name, e.g. :data:`RC_ENVIRONMENT`.
    :returns: A Concourse ``s3`` resource whose versions are release versions.
    """
    return Resource(
        name=name,
        type="s3",
        icon="rocket-launch",
        source={
            "bucket": DEPLOY_MARKER_BUCKET,
            "region_name": "us-east-1",
            "regexp": deploy_marker_regexp(app_name, environment),
            # Use the worker instance role rather than falling back to anonymous
            # credentials. No key material is involved anywhere in this contract.
            "enable_aws_creds_provider": True,
            # See INITIAL_MARKER_VERSION. initial_path must itself match the
            # regexp above or the s3 resource rejects the configuration.
            "initial_path": (
                f"{deploy_marker_directory(app_name, environment)}/"
                f"{deploy_marker_filename(INITIAL_MARKER_VERSION)}"
            ),
            "initial_content_text": json.dumps(
                {
                    "app": app_name,
                    "environment": environment,
                    "version": INITIAL_MARKER_VERSION,
                    # Distinguishes "no deploy has been recorded yet" from a
                    # real release, for anything that reads the body.
                    "synthetic": True,
                }
            ),
        },
    )
