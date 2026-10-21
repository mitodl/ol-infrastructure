"""Tests for the format-14 stop window and the pre-deploy plan gate.

Both run commands baked into the omnigraph-server image (agent-kit
``docker/omnigraph_maintenance.py`` and ``docker/omnigraph_plan_gate.py``), so
what this stack can get wrong is the contract: an env name the command does
not read, a verb its ServiceAccount lacks, or a time budget in which the Job's
deadline kills the window before it has scaled the server back up.
"""

from typing import Any

import pulumi

from ol_infrastructure.applications.omnigraph.lock_handoff import (
    MIGRATION_ARMED_ANNOTATION,
    create_plan_gate,
)
from ol_infrastructure.applications.omnigraph.maintenance import (
    COMMAND_TIMEOUT_SECONDS,
    MAINTENANCE_WINDOW_SECONDS,
    TERMINATION_GRACE_SECONDS,
    WINDOW_ACTIVE_DEADLINE_SECONDS,
    WINDOW_STARTING_DEADLINE_SECONDS,
    create_maintenance_window,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

STACK_INFO = StackInfo(
    name="CI",
    namespace="",
    env_suffix="ci",
    env_prefix="",
    full_name="organization/ol-application-omnigraph/CI",
    k8s_name="ci",
)

# 03:20 UTC start, and the CI indexer's first run after it at 04:00.
SECONDS_TO_NEXT_WRITER = 40 * 60


class _Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        return [f"{args.name}_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def _of_type(mocks: _Mocks, typ: str) -> list[dict[str, Any]]:
    return [r.inputs for r in mocks.resources if r.typ == typ]


def _window(
    *, suspend: bool = False, cleanup_iso_weekdays: tuple[int, ...] = (1, 3, 7)
):
    return create_maintenance_window(
        stack_info=STACK_INFO,
        namespace="omnigraph",
        k8s_global_labels={},
        image="omnigraph-server@sha256:abc",
        aws_region="us-east-1",
        cluster_configmap_name="omnigraph-cluster-config",
        cluster_config_dir="/etc/omnigraph/cluster",
        server_deployment_name="omnigraph-server",
        server_termination_grace_seconds=125,
        schedule="20 3 * * *",
        cleanup_older_than="30d",
        cleanup_iso_weekdays=cleanup_iso_weekdays,
        depends_on=[],
        suspend=suspend,
    )


def test_the_window_closes_itself_before_the_deadline_and_the_next_writer() -> None:
    """The command enforces the first two. The Job deadline is the backstop, and
    a kill at the end of the grace period still precedes the 04:00 indexer,
    also for a run that started as late as it is allowed to.
    """
    assert WINDOW_ACTIVE_DEADLINE_SECONDS > (
        MAINTENANCE_WINDOW_SECONDS + COMMAND_TIMEOUT_SECONDS
    )
    assert (
        WINDOW_STARTING_DEADLINE_SECONDS
        + WINDOW_ACTIVE_DEADLINE_SECONDS
        + TERMINATION_GRACE_SECONDS
        < SECONDS_TO_NEXT_WRITER
    )


def test_the_grace_period_is_what_the_image_says_its_close_needs() -> None:
    """agent-kit docker/README.md: on the default 30s the pod is killed before
    it has scaled the server back up.
    """
    assert TERMINATION_GRACE_SECONDS >= 120


@pulumi.runtime.test
def test_the_window_can_scale_the_server_and_nothing_else_about_it():
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)
    window = _window()

    def check(_: object) -> None:
        (role,) = _of_type(mocks, "kubernetes:rbac.authorization.k8s.io/v1:Role")
        assert role["rules"] == [
            {
                "apiGroups": ["coordination.k8s.io"],
                "resources": ["leases"],
                "resourceNames": ["omnigraph-unlock"],
                "verbs": ["get", "update"],
            },
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
            {
                "apiGroups": ["apps"],
                "resources": ["deployments"],
                "resourceNames": ["omnigraph-server"],
                "verbs": ["get"],
            },
            {
                "apiGroups": ["apps"],
                "resources": ["deployments/scale"],
                "resourceNames": ["omnigraph-server"],
                "verbs": ["get", "update"],
            },
        ]
        (binding,) = _of_type(
            mocks, "kubernetes:rbac.authorization.k8s.io/v1:RoleBinding"
        )
        # Its own identity: bound to the server's, the server could scale
        # its own Deployment.
        assert binding["subjects"] == [
            {
                "kind": "ServiceAccount",
                "name": "omnigraph-maintenance",
                "namespace": "omnigraph",
            }
        ]

    return window.role_binding.urn.apply(check)


@pulumi.runtime.test
def test_the_cron_job_runs_the_images_window_command_with_its_env():
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)
    window = _window()

    def check(_: object) -> None:
        (cron_job,) = _of_type(mocks, "kubernetes:batch/v1:CronJob")
        spec = cron_job["spec"]
        assert spec["schedule"] == "20 3 * * *"
        assert spec["concurrencyPolicy"] == "Forbid"
        assert spec["suspend"] is False
        job = spec["jobTemplate"]["spec"]
        # A retry would stop the server a second time.
        assert job["backoffLimit"] == 0
        assert job["activeDeadlineSeconds"] == WINDOW_ACTIVE_DEADLINE_SECONDS
        pod = job["template"]["spec"]
        assert pod["serviceAccountName"] == "omnigraph-maintenance"
        assert pod["terminationGracePeriodSeconds"] == TERMINATION_GRACE_SECONDS
        (container,) = pod["containers"]
        assert container["command"] == ["/usr/local/bin/omnigraph_maintenance.py"]
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env == {
            "AWS_REGION": "us-east-1",
            "OMNIGRAPH_CLUSTER_DIR": "/etc/omnigraph/cluster",
            "OMNIGRAPH_SERVER_DEPLOYMENT": "omnigraph-server",
            "OMNIGRAPH_UNLOCK_LEASE": "omnigraph-unlock",
            "POD_NAME": None,
            "OMNIGRAPH_CLEANUP_OLDER_THAN": "30d",
            "OMNIGRAPH_CLEANUP_ISO_WEEKDAY": "1,3,7",
            "OMNIGRAPH_WINDOW_SECONDS": str(MAINTENANCE_WINDOW_SECONDS),
            "OMNIGRAPH_COMMAND_TIMEOUT_SECONDS": str(COMMAND_TIMEOUT_SECONDS),
            # A server draining writes for its whole grace is not stuck.
            "OMNIGRAPH_SERVER_STOP_SECONDS": "180",
        }
        # The command reads the storage root and the graph list from it.
        (mount,) = container["volumeMounts"]
        assert mount["mountPath"] == "/etc/omnigraph/cluster/cluster.yaml"
        assert mount["subPath"] == "cluster.yaml"
        # The server's pods must not match the window's own pod, or the wait
        # for the server to stop would wait on itself.
        labels = job["template"]["metadata"]["labels"]
        assert labels["app.kubernetes.io/name"] == "omnigraph-maintenance"

    return window.cron_job.urn.apply(check)


@pulumi.runtime.test
def test_no_cleanup_days_render_as_an_empty_list():
    """The image reads an empty OMNIGRAPH_CLEANUP_ISO_WEEKDAY as no cleanup days.

    agent-kit docker/omnigraph_maintenance.py parse_iso_weekdays. Production
    sets ``omnigraph:cleanup_iso_weekdays: []`` so its window only optimizes.
    """
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)
    window = _window(cleanup_iso_weekdays=())

    def check(_: object) -> None:
        (cron_job,) = _of_type(mocks, "kubernetes:batch/v1:CronJob")
        (container,) = cron_job["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ]
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["OMNIGRAPH_CLEANUP_ISO_WEEKDAY"] == ""

    return window.cron_job.urn.apply(check)


@pulumi.runtime.test
def test_an_armed_migration_suspends_the_window():
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)
    window = _window(suspend=True)

    def check(_: object) -> None:
        (cron_job,) = _of_type(mocks, "kubernetes:batch/v1:CronJob")
        assert cron_job["spec"]["suspend"] is True

    return window.cron_job.urn.apply(check)


def test_the_armed_marker_is_the_one_the_image_looks_for() -> None:
    """agent-kit docker/omnigraph_maintenance.py MIGRATION_ARMED_ANNOTATION."""
    assert MIGRATION_ARMED_ANNOTATION == "ol.mit.edu/migration-armed"


@pulumi.runtime.test
def test_the_plan_gate_runs_the_images_plan_check_with_the_boots_inputs():
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)
    gate = create_plan_gate(
        stack_info=STACK_INFO,
        namespace="omnigraph",
        k8s_global_labels={},
        image="omnigraph-server@sha256:abc",
        aws_region="us-east-1",
        service_account_name="omnigraph-server",
        cluster_configmap_name="omnigraph-cluster-config",
        cluster_config_dir="/etc/omnigraph/cluster",
        actor_tokens_secret_name="omnigraph-actor-tokens",  # pragma: allowlist secret
        actor_tokens_file="/etc/omnigraph/tokens/tokens.json",
        apply_actor="svc-witan-admin",
        graph_removal_allowlist=["code-a"],
        config_hash=pulumi.Output.from_input("abc123"),
        depends_on=[],
    )

    def check(_: object) -> None:
        (job,) = _of_type(mocks, "kubernetes:batch/v1:Job")
        assert "name" not in job["metadata"]
        template = job["spec"]["template"]
        # A new cluster.yaml or image has to run the gate again.
        assert template["metadata"]["annotations"] == {
            "ol.mit.edu/config-hash": "abc123"
        }
        (container,) = template["spec"]["containers"]
        assert container["command"] == ["/usr/local/bin/omnigraph_plan_gate.py"]
        env = {e["name"]: e["value"] for e in container["env"]}
        assert env == {
            "AWS_REGION": "us-east-1",
            "OMNIGRAPH_CLUSTER_DIR": "/etc/omnigraph/cluster",
            "OMNIGRAPH_SERVER_BEARER_TOKENS_FILE": "/etc/omnigraph/tokens/tokens.json",
            "OMNIGRAPH_APPLY_ACTOR": "svc-witan-admin",
            "OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST": "code-a",
        }
        mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
        assert mounts == {
            "cluster-config": "/etc/omnigraph/cluster/cluster.yaml",
            "actor-tokens": "/etc/omnigraph/tokens",
        }

    return gate.urn.apply(check)
