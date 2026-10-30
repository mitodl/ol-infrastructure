import pytest

from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCSecretCredentials,
    OLApisixOIDCVaultCredentials,
)
from ol_infrastructure.lib.app_bindings import (
    WorkloadResources,
    celery_worker_configs,
    oidc_credentials,
    validate_queue_sizing,
)


def test_merged_worker_takes_the_largest_queue():
    big = WorkloadResources(requests={"memory": "2Gi"}, limits={"memory": "2Gi"})
    small = WorkloadResources(requests={"memory": "1Gi"}, limits={"memory": "1Gi"})
    (worker,) = celery_worker_configs(["a", "b"], "merged", {"a": big, "b": small})
    assert worker.worker_name == "all"
    assert worker.queue_name == "a,b"
    assert worker.resource_requests == big.requests
    assert worker.resource_limits == {"memory": "2Gi"}


def test_an_uncapped_queue_is_the_largest():
    capped = WorkloadResources(requests={"memory": "2Gi"}, limits={"memory": "2Gi"})
    uncapped = WorkloadResources(requests={"memory": "1Gi"}, limits={})
    (worker,) = celery_worker_configs(
        ["a", "b"], "merged", {"a": capped, "b": uncapped}
    )
    assert worker.resource_limits == {}


def test_per_queue_runs_one_worker_per_queue_in_order():
    size = WorkloadResources(requests={"memory": "1Gi"}, limits={"memory": "1Gi"})
    workers = celery_worker_configs(["b", "a"], "per-queue", {"a": size, "b": size})
    assert [w.queue_name for w in workers] == ["b", "a"]


@pytest.mark.parametrize(
    ("queues", "complaint"),
    [
        (["a"], r"missing \['b'\]"),
        (["a", "b", "extra"], r"unknown \['extra'\]"),
    ],
)
def test_queue_sizing_must_name_every_queue_exactly_once(queues, complaint):
    size = WorkloadResources(requests={"memory": "1Gi"}, limits={"memory": "1Gi"})
    with pytest.raises(ValueError, match=complaint):
        validate_queue_sizing(dict.fromkeys(queues, size), ["a", "b"])


def test_oidc_credentials_from_a_secret():
    name = "ol-x-oidc"
    creds = oidc_credentials(vault_auth_name=None, secret_name=name, vault_path="sso/x")
    assert isinstance(creds, OLApisixOIDCSecretCredentials)
    assert creds.secret_name == name


def test_oidc_credentials_from_vault():
    creds = oidc_credentials(
        vault_auth_name="x-auth", secret_name=None, vault_path="sso/x"
    )
    assert isinstance(creds, OLApisixOIDCVaultCredentials)
    assert (creds.path, creds.vaultauth) == ("sso/x", "x-auth")


@pytest.mark.parametrize(
    ("vault_auth_name", "secret_name"), [(None, None), ("x-auth", "ol-x-oidc")]
)
def test_oidc_credentials_need_exactly_one_source(vault_auth_name, secret_name):
    with pytest.raises(ValueError, match="oidc_secret_name"):
        oidc_credentials(
            vault_auth_name=vault_auth_name,
            secret_name=secret_name,
            vault_path="sso/x",
        )
