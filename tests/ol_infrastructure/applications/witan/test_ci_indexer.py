"""The CI indexer's CronJob as Pulumi declares it, rendered against mocks."""

import pulumi
import pulumi_kubernetes as kubernetes

from ol_infrastructure.applications.witan.ci_indexer import (
    CRONJOB_NAME,
    DEFAULT_INDEX_SCHEDULE,
    create_ci_indexer,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo
from tests.ol_infrastructure.recording_mocks import RecordingMocks

FOUR_HOURS_SECONDS = 4 * 60 * 60


def _render() -> tuple[RecordingMocks, kubernetes.batch.v1.CronJob]:
    mocks = RecordingMocks()
    pulumi.runtime.set_mocks(mocks, preview=False)

    token_secret = kubernetes.core.v1.Secret(
        "witan-ci-token",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(name="witan-ci-token"),
    )
    indexer = create_ci_indexer(
        stack_info=StackInfo(
            name="CI",
            namespace="",
            env_suffix="ci",
            env_prefix="",
            full_name="organization/ol-application-witan/CI",
            k8s_name="ci",
        ),
        namespace="witan",
        k8s_global_labels={},
        witan_image="witan@sha256:abc",
        omnigraph_server_addr="http://omnigraph-server:8080",
        managed_repos=["https://github.com/mitodl/ol-infrastructure"],
        schedule=DEFAULT_INDEX_SCHEDULE,
        witan_ci_token_secret_name="witan-ci-token",  # pragma: allowlist secret
        witan_ci_token_secret_key="token",  # pragma: allowlist secret
        witan_ci_token_secret=token_secret,
        service_version="sha256:abc",
    )
    assert indexer is not None
    return mocks, indexer


@pulumi.runtime.test
def test_a_failed_run_outlives_the_next_run():
    """The next run has finished, one way or the other, before the TTL removes
    the failure, so consecutive failures never leave a gap that resolves the
    alert and re-fires it.
    """
    assert DEFAULT_INDEX_SCHEDULE == "0 */4 * * *"
    mocks, indexer = _render()

    def check(_: object) -> None:
        (cron_job,) = mocks.of_type("kubernetes:batch/v1:CronJob")
        assert cron_job["metadata"]["name"] == CRONJOB_NAME
        spec = cron_job["spec"]
        job_spec = spec["jobTemplate"]["spec"]
        # The latest the next run can finish: it may start up to the starting
        # deadline late, then run up to its active deadline.
        next_run_done = (
            FOUR_HOURS_SECONDS
            + spec["startingDeadlineSeconds"]
            + job_spec["activeDeadlineSeconds"]
        )
        assert job_spec["ttlSecondsAfterFinished"] > next_run_done

    return indexer.urn.apply(check)
