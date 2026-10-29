"""Which resources ``create_data_tier`` creates for each storage format.

The builders have their own tests. What those cannot see is the gating: a plan
gate created while a migration is armed fails the update it is part of, one
missing at format 14 lets a refused apply become an outage, and a stop window
on format 9 would force-unlock a lock that binary never keeps. The whole
function is run here under Pulumi mocks, once per case.
"""

from types import SimpleNamespace
from typing import Any

import pulumi
import pulumi_kubernetes as kubernetes
import pytest

from ol_infrastructure.applications.omnigraph import data_tier
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import StackInfo

STACK_INFO = StackInfo(
    name="CI",
    namespace="",
    env_suffix="ci",
    env_prefix="",
    full_name="organization/ol-application-omnigraph/CI",
    k8s_name="ci",
)
JOB = "kubernetes:batch/v1:Job"
CRON_JOB = "kubernetes:batch/v1:CronJob"
DEPLOYMENT = "kubernetes:apps/v1:Deployment"


class _Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        return [f"{args.name}_id", {**args.inputs, "arn": f"arn:aws:::{args.name}"}]

    def call(self, args: pulumi.runtime.MockCallArgs):
        if args.token == "aws:index/getCallerIdentity:getCallerIdentity":  # noqa: S105
            return {"accountId": "123456789012"}
        return {}

    def of_type(self, typ: str) -> list[dict[str, Any]]:
        return [r.inputs for r in self.resources if r.typ == typ]

    def named(self, typ: str) -> dict[str, dict[str, Any]]:
        return {
            r.inputs["metadata"].get("name", r.name): r.inputs
            for r in self.resources
            if r.typ == typ
        }


def _build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    served_format: int,
    image_format: int,
    migration_armed: bool = False,
    boot_budget_seconds: int | None = None,
) -> tuple[_Mocks, data_tier.OmnigraphDataTier]:
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)
    monkeypatch.setenv("OMNIGRAPH_DOCKER_SHA", "sha256:" + "a" * 64)
    monkeypatch.delenv("OMNIGRAPH_DOCKER_TAG", raising=False)
    monkeypatch.setattr(
        data_tier, "read_image_internal_schema", lambda *_: image_format
    )
    service_account = kubernetes.core.v1.ServiceAccount("omnigraph-server-sa")
    tier = data_tier.create_data_tier(
        stack_info=STACK_INFO,
        namespace="omnigraph",
        k8s_global_labels={},
        aws_config=AWSBase(tags={"OU": "operations", "Environment": "ci"}),
        auth_binding=SimpleNamespace(
            irsa_role=SimpleNamespace(name="omnigraph-role"),
            irsa_service_accounts=[service_account],
        ),
        actor_tokens_secret_name="omnigraph-actor-tokens",  # pragma: allowlist secret
        actor_tokens_secret=kubernetes.core.v1.Secret("actor-tokens"),
        managed_repos=["https://github.com/mitodl/ol-django"],
        optimize_schedule="20 3 * * *",
        cleanup_schedule="20 4 * * 0",
        cleanup_older_than="30d",
        internal_schema_version=served_format,
        migrate_to_prefix=f"fmt{image_format}" if migration_armed else "",
        suspend_maintenance=migration_armed,
        migration_armed=migration_armed,
        boot_budget_seconds=boot_budget_seconds,
    )
    return mocks, tier


def _job_commands(mocks: _Mocks) -> list[list[str]]:
    return [
        job["spec"]["template"]["spec"]["containers"][0]["command"]
        for job in mocks.of_type(JOB)
    ]


@pulumi.runtime.test
def test_format_9_keeps_the_apply_job_and_the_two_sweeps(monkeypatch):
    mocks, tier = _build(monkeypatch, served_format=9, image_format=9)

    def check(_: object) -> None:
        assert _job_commands(mocks) == [["omnigraph"]]
        assert tier.plan_gate_job is None
        assert tier.cluster_apply_job is not None
        cron_jobs = mocks.named(CRON_JOB)
        assert sorted(cron_jobs) == ["omnigraph-cleanup", "omnigraph-optimize"]
        assert all(cj["spec"]["suspend"] is False for cj in cron_jobs.values())
        assert not mocks.of_type("kubernetes:coordination.k8s.io/v1:Lease")
        (deployment,) = mocks.of_type(DEPLOYMENT)
        # No annotations key at all: an empty map would be an update on every
        # format-9 stack.
        assert "annotations" not in deployment["metadata"]
        assert deployment["spec"]["replicas"] == 1

    return pulumi.Output.all(
        tier.deployment.urn, *(cj.urn for cj in tier.maintenance.cron_jobs)
    ).apply(check)


@pulumi.runtime.test
def test_format_14_has_the_plan_gate_and_one_stop_window(monkeypatch):
    mocks, tier = _build(monkeypatch, served_format=14, image_format=14)

    def check(_: object) -> None:
        assert _job_commands(mocks) == [["/usr/local/bin/omnigraph_plan_gate.py"]]
        assert tier.cluster_apply_job is None
        assert tier.plan_gate_job is not None
        cron_jobs = mocks.named(CRON_JOB)
        assert list(cron_jobs) == ["omnigraph-maintenance"]
        assert cron_jobs["omnigraph-maintenance"]["spec"]["suspend"] is False
        assert len(tier.maintenance.cron_jobs) == 1
        assert mocks.of_type("kubernetes:coordination.k8s.io/v1:Lease")
        (deployment,) = mocks.of_type(DEPLOYMENT)
        assert deployment["spec"]["replicas"] == 1
        # The window writes spec.replicas through the scale subresource.
        # Without this a later update that changes the count (arming a
        # migration) is refused as a field conflict.
        assert deployment["metadata"]["annotations"] == {
            "pulumi.com/patchForce": "true"
        }
        # No boot budget, no new inputs: the spec a deploy without one sees.
        assert "progressDeadlineSeconds" not in deployment["spec"]
        (container,) = deployment["spec"]["template"]["spec"]["containers"]
        assert container["startupProbe"]["failureThreshold"] == 24

    return pulumi.Output.all(
        tier.deployment.urn,
        tier.plan_gate_job.urn,
        *(cj.urn for cj in tier.maintenance.cron_jobs),
    ).apply(check)


@pulumi.runtime.test
def test_the_deployment_waits_for_the_plan_gate(monkeypatch):
    """A gate the Deployment did not depend on would fail after the old pod
    had already been stopped.
    """
    depends_on: list[pulumi.Resource] = []
    real_deployment = kubernetes.apps.v1.Deployment

    def recording_deployment(*args: Any, **kwargs: Any):
        depends_on.extend(kwargs["opts"].depends_on)
        return real_deployment(*args, **kwargs)

    monkeypatch.setattr(kubernetes.apps.v1, "Deployment", recording_deployment)
    _, tier = _build(monkeypatch, served_format=14, image_format=14)
    assert tier.plan_gate_job is not None
    assert any(resource is tier.plan_gate_job for resource in depends_on)
    return tier.deployment.urn


@pulumi.runtime.test
def test_an_armed_migration_from_format_14_has_no_gate_and_marks_the_deployment(
    monkeypatch,
):
    """The new image cannot read the served root, so a plan could only fail."""
    mocks, tier = _build(
        monkeypatch, served_format=14, image_format=15, migration_armed=True
    )

    def check(_: object) -> None:
        assert mocks.of_type(JOB) == []
        assert tier.plan_gate_job is None
        assert tier.cluster_apply_job is None
        (cron_job,) = mocks.of_type(CRON_JOB)
        assert cron_job["metadata"]["name"] == "omnigraph-maintenance"
        assert cron_job["spec"]["suspend"] is True
        (deployment,) = mocks.of_type(DEPLOYMENT)
        assert deployment["spec"]["replicas"] == 0
        assert deployment["metadata"]["annotations"] == {
            "ol.mit.edu/migration-armed": "true",
            "pulumi.com/patchForce": "true",
        }

    return pulumi.Output.all(
        tier.deployment.urn, *(cj.urn for cj in tier.maintenance.cron_jobs)
    ).apply(check)


@pulumi.runtime.test
def test_an_armed_migration_from_format_9_keeps_the_sweeps_suspended(monkeypatch):
    """The cutover itself: the old sweeps exist until the format moves to 14."""
    mocks, tier = _build(
        monkeypatch, served_format=9, image_format=14, migration_armed=True
    )

    def check(_: object) -> None:
        assert mocks.of_type(JOB) == []
        cron_jobs = mocks.named(CRON_JOB)
        assert sorted(cron_jobs) == ["omnigraph-cleanup", "omnigraph-optimize"]
        assert all(cj["spec"]["suspend"] is True for cj in cron_jobs.values())
        (deployment,) = mocks.of_type(DEPLOYMENT)
        assert deployment["spec"]["replicas"] == 0
        assert deployment["metadata"]["annotations"] == {
            "ol.mit.edu/migration-armed": "true"
        }

    return pulumi.Output.all(
        tier.deployment.urn, *(cj.urn for cj in tier.maintenance.cron_jobs)
    ).apply(check)


@pulumi.runtime.test
def test_a_boot_budget_lengthens_the_probe_and_both_rollout_waits(monkeypatch):
    """A withheld graph's delete runs in the boot apply, before the server
    listens. CI's 15,438-object code-bridge delete was killed by the default
    probe with 11,356 objects left.
    """
    mocks, tier = _build(
        monkeypatch, served_format=14, image_format=14, boot_budget_seconds=3600
    )

    def check(_: object) -> None:
        (deployment,) = mocks.of_type(DEPLOYMENT)
        (container,) = deployment["spec"]["template"]["spec"]["containers"]
        # 20 + (717 - 1) x 5 = 3600.
        assert container["startupProbe"]["failureThreshold"] == 717
        # 125s termination grace + 3600s boot + 300s slack.
        assert deployment["spec"]["progressDeadlineSeconds"] == 4025
        assert deployment["metadata"]["annotations"] == {
            "pulumi.com/patchForce": "true",
            "pulumi.com/timeoutSeconds": "4025",
        }

    return tier.deployment.urn.apply(check)


@pytest.mark.parametrize(
    ("budget", "threshold"),
    [(None, 24), (135, 24), (136, 25), (140, 25), (1800, 357)],
)
def test_the_failure_threshold_kills_no_sooner_than_the_budget(budget, threshold):
    assert data_tier.startup_probe_failure_threshold(budget) == threshold


def test_a_boot_budget_shorter_than_the_default_is_refused():
    with pytest.raises(ValueError, match="shorter than the default 135s"):
        data_tier.startup_probe_failure_threshold(120)
