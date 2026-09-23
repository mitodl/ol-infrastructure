"""The migration Job as Pulumi declares it, rendered against mocks.

The pre-flight needs three things from the declaration that nothing else
checks until a real migration is armed, which is the worst time to find out:
the four writers it waits on, read access to Jobs and CronJobs in BOTH
namespaces, and the ``__cluster`` snapshot running before the migrate
container.
"""

import os
import subprocess
from pathlib import Path
from typing import Any

import pulumi
import pulumi_kubernetes as kubernetes

from ol_infrastructure.applications.omnigraph.maintenance import OmnigraphMaintenance
from ol_infrastructure.applications.omnigraph.storage_migration import (
    CLUSTER_SNAPSHOT_SCRIPT,
    create_storage_migration,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


class _Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        return f"{args.name}_id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def _of_type(mocks: _Mocks, typ: str) -> list[dict[str, Any]]:
    return [r.inputs for r in mocks.resources if r.typ == typ]


def _cron_job(name: str) -> kubernetes.batch.v1.CronJob:
    return kubernetes.batch.v1.CronJob(
        name,
        metadata=kubernetes.meta.v1.ObjectMetaArgs(name=name, namespace="omnigraph"),
        spec=kubernetes.batch.v1.CronJobSpecArgs(
            schedule="0 0 * * *",
            job_template=kubernetes.batch.v1.JobTemplateSpecArgs(
                spec=kubernetes.batch.v1.JobSpecArgs(
                    template=kubernetes.core.v1.PodTemplateSpecArgs(
                        spec=kubernetes.core.v1.PodSpecArgs(containers=[])
                    )
                )
            ),
        ),
    )


@pulumi.runtime.test
def test_the_armed_job_can_check_and_snapshot_before_migrating():
    """Roles in both namespaces, all four writers named, snapshot first."""
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)

    migration = create_storage_migration(
        stack_info=StackInfo(
            name="CI",
            namespace="",
            env_suffix="ci",
            env_prefix="",
            full_name="organization/ol-application-omnigraph/CI",
            k8s_name="ci",
        ),
        namespace="omnigraph",
        k8s_global_labels={},
        old_image="old@sha256:abc",
        new_image="new@sha256:def",
        old_storage_root="s3://ol-data-witan-ci/fmt9",
        new_storage_root="s3://ol-data-witan-ci/fmt10",
        new_storage_prefix="fmt10",
        cluster_configmap_name="omnigraph-cluster",
        service_account_name="omnigraph-server",
        backup_root="s3://ol-data-witan-ci/backups",
        aws_cli_image="aws-cli:test",
        witan_namespace="witan",
        witan_writer_cronjobs=["witan-ci-indexer", "witan-view-reaper"],
        maintenance=OmnigraphMaintenance(
            optimize=_cron_job("omnigraph-optimize"),
            cleanup=_cron_job("omnigraph-cleanup"),
        ),
    )

    def check(_: object) -> None:
        roles = _of_type(mocks, "kubernetes:rbac.authorization.k8s.io/v1:Role")
        assert sorted(r["metadata"]["namespace"] for r in roles) == [
            "omnigraph",
            "witan",
        ]
        for role in roles:
            assert role["rules"] == [
                {
                    "apiGroups": ["batch"],
                    "resources": ["cronjobs", "jobs"],
                    "verbs": ["get", "list"],
                }
            ]
        bindings = _of_type(
            mocks, "kubernetes:rbac.authorization.k8s.io/v1:RoleBinding"
        )
        assert {b["metadata"]["namespace"] for b in bindings} == {"omnigraph", "witan"}
        for binding in bindings:
            assert binding["subjects"] == [
                {
                    "kind": "ServiceAccount",
                    "name": "omnigraph-server",
                    "namespace": "omnigraph",
                }
            ]

        (job,) = _of_type(mocks, "kubernetes:batch/v1:Job")
        pod = job["spec"]["template"]["spec"]
        assert [c["name"] for c in pod["initContainers"]] == [
            "stage-old-binary",
            "snapshot-cluster-state",
        ]
        snapshot_env = {e["name"]: e["value"] for e in pod["initContainers"][1]["env"]}
        assert snapshot_env["OLD_ROOT"] == "s3://ol-data-witan-ci/fmt9"
        assert snapshot_env["BACKUP_ROOT"] == "s3://ol-data-witan-ci/backups"
        assert snapshot_env["NEW_PREFIX"] == "fmt10"

        migrate_env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
        assert migrate_env["OMNIGRAPH_WRITER_CRONJOBS"].split() == [
            "omnigraph/omnigraph-optimize",
            "omnigraph/omnigraph-cleanup",
            "witan/witan-ci-indexer",
            "witan/witan-view-reaper",
        ]

    return migration.job.urn.apply(check)


def _run_snapshot(tmp_path: Path, listing: str) -> subprocess.CompletedProcess[str]:
    """Run the snapshot script against a stub ``aws`` that lists ``listing``."""
    (tmp_path / "listing").write_text(listing)
    stub = tmp_path / "aws"
    stub.write_text(f'#!/bin/sh\n[ "$2" = ls ] && cat {tmp_path / "listing"}\nexit 0\n')
    stub.chmod(0o755)
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "BACKUP_ROOT": "s3://bucket/backups",
        "NEW_PREFIX": "fmt9",
        "OLD_ROOT": "s3://bucket/fmt6",
    }
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", CLUSTER_SNAPSHOT_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_an_empty_snapshot_fails_the_job(tmp_path: Path) -> None:
    """A sync of a missing ``__cluster`` copies nothing, and ``printf | wc -l``
    would count the empty listing as one object.
    """
    result = _run_snapshot(tmp_path, "")

    assert result.returncode == 1
    assert "is empty" in result.stderr


def test_a_populated_snapshot_reports_its_object_count(tmp_path: Path) -> None:
    result = _run_snapshot(tmp_path, "2026-09-22 a\n2026-09-22 b\n")

    assert result.returncode == 0
    assert "snapshot: 2 object(s)" in result.stdout
