"""Pin the VSO resources mitxonline's deployed program creates.

``vso_secrets_main.json`` was generated once by ``capture`` from k8s_secrets.py
as it stood on main at dd285f077, before the variables were described as data.
It proves the description reproduces that file; a difference here is a change
to what deployed stacks get, so regenerate the fixture only on purpose.
"""

import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from ol_infrastructure.components.services.vault import OLVaultRestartTarget
from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.secret_env import vso_template
from tests.ol_infrastructure.applications.vso_capture import (
    MountSpec,
    capture_vso_secrets,
)

FIXTURE = Path(__file__).parent / "fixtures" / "vso_secrets_main.json"
DEPLOYMENTS = ["mitxonline-app", "mitxonline-celery-beat"]


def capture(module: ModuleType, starrocks_mount: str | None = None) -> dict[str, Any]:
    """Call ``module.create_mitxonline_k8s_secrets`` with QA-shaped inputs.

    :param starrocks_mount: Passed through; QA, which the fixture records, has none.
    """
    return capture_vso_secrets(
        project="ol-application-mitxonline",
        stack="QA",
        mounts={"db": MountSpec("postgres-mitxonline", "database", "<database mount>")},
        create=lambda mounts, cache: module.create_mitxonline_k8s_secrets(
            stack_info=StackInfo(
                name="QA",
                namespace="applications.mitxonline",
                env_suffix="qa",
                env_prefix="applications",
                full_name="applications.mitxonline.QA",
                k8s_name="qa",
            ),
            mitxonline_namespace="mitxonline",
            k8s_global_labels={"ol.mit.edu/application": "mitxonline"},
            vault_k8s_resources=SimpleNamespace(auth_name="mitxonline"),
            db_config=SimpleNamespace(db_mount=mounts["db"]),
            rds_endpoint="mitxonline-db.example:5432",
            openedx_environment="mitxonline-qa",
            redis_password="redis-password",  # pragma: allowlist secret
            redis_cache=cache,
            starrocks_mount=starrocks_mount,
            starrocks_restart_targets=[
                OLVaultRestartTarget(kind="Deployment", name=name)
                for name in DEPLOYMENTS
            ],
        ),
    )


def test_the_vso_resources_match_main_before_the_description():
    # Imported here, not at module level: k8s_secrets pulls in an ElastiCache
    # helper that builds a boto3 client on import, and conftest only sets
    # AWS_REGION once collection is over.
    from ol_infrastructure.applications.mitxonline import (  # noqa: PLC0415
        k8s_secrets,
    )

    captured = capture(k8s_secrets)
    assert captured.pop("optional_env_from_secret_names") == []
    assert captured == json.loads(FIXTURE.read_text())


def test_starrocks_credentials_are_an_optional_dynamic_secret():
    """With a StarRocks mount, the read-only credentials are one more dynamic
    secret, which pods mount as optional so it cannot hold up a rollout, and
    whose rotation restarts the Deployments, which read it through envFrom.
    """
    from ol_infrastructure.applications.mitxonline import (  # noqa: PLC0415
        k8s_secrets,
    )

    without = capture(k8s_secrets)
    captured = capture(k8s_secrets, starrocks_mount="database-starrocks")

    assert captured["optional_env_from_secret_names"] == [
        "starrocks-mitxonline-dynamic-secret"
    ]
    assert captured["env_from_secret_names"] == without["env_from_secret_names"]
    [starrocks] = [
        config
        for config in captured["vso_secrets"]
        if config["mount"] == "database-starrocks"
    ]
    assert starrocks["config_class"] == "OLVaultK8SDynamicSecretConfig"
    assert starrocks["path"] == "creds/readonly"
    assert starrocks["templates"] == [
        ["STARROCKS_USER", vso_template(("username",))],
        ["STARROCKS_PASSWORD", vso_template(("password",))],
    ]
    assert starrocks["restart_targets"] == [
        {"kind": "Deployment", "name": name} for name in DEPLOYMENTS
    ]
    assert [
        config["resource_name"]
        for config in captured["vso_secrets"]
        if config["restart_targets"]
    ] == [starrocks["resource_name"]]
