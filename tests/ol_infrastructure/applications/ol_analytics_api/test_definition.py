"""Tests for the ol-analytics-api application definition and its secrets description."""

import pytest

from bridge.lib.constants import LearnSession
from ol_infrastructure.applications.ol_analytics_api import definition, vault_secrets
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCSecretCredentials,
    OLApisixOIDCVaultCredentials,
)
from ol_infrastructure.lib.secret_env import Template, VaultSecret

OIDC_SECRET = "ol-mitlearn-oidc"  # noqa: S105  # pragma: allowlist secret
VAULT_ENV = {
    "OL_ANALYTICS_API_VAULT_ADDR",
    "OL_ANALYTICS_API_VAULT_ROLE",
    "OL_ANALYTICS_API_VAULT_STARROCKS_MOUNT",
    "OL_ANALYTICS_API_VAULT_K8S_MOUNT",
}


def _bindings(**overrides):
    fields = {
        "env_suffix": "qa",
        "hostnames": definition.OlAnalyticsApiHostnames(
            canonical="analytics-qa.ol.mit.edu", learn="analytics.rc.learn.mit.edu"
        ),
        "environment_variables": {"LOG_LEVEL": "INFO"},
        "starrocks_vault": definition.StarRocksVaultLogin(
            address="https://vault-qa.odl.mit.edu", k8s_auth_mount="k8s-data"
        ),
        "cors_frontend_origins": ["https://rc.learn.mit.edu"],
        "k8s_labels": {"ol.mit.edu/service": "ol-analytics-api"},
        "secret_names": ["s"],
        "application_docker_tag": "latest",
        "cluster": definition.ClusterCapabilities(vault_auth_name="vaultauth"),
        "min_replicas": 2,
    }
    return definition.OlAnalyticsApiBindings(**(fields | overrides))


def test_the_vault_login_variables_come_only_with_a_vault_login():
    """Without them the app reads STARROCKS_USER/STARROCKS_PASSWORD instead."""
    deployed = definition.environment_variables(_bindings())
    assert set(deployed) >= VAULT_ENV
    assert deployed["OL_ANALYTICS_API_VAULT_ROLE"] == "ol-analytics-api"
    assert deployed["OL_ANALYTICS_API_VAULT_STARROCKS_MOUNT"] == "database-starrocks"
    local = definition.environment_variables(_bindings(starrocks_vault=None))
    assert not VAULT_ENV & set(local)
    assert local["OTEL_RESOURCE_ATTRIBUTES"] == "ol.mit.edu/service=ol-analytics-api"


def test_synced_source_reloads_and_leaves_the_root_writable():
    deployed = definition.application_config(_bindings())
    assert deployed.granian_config is not None
    assert deployed.granian_config.reload is False
    assert deployed.container_security_context is not None
    assert deployed.container_security_context.read_only_root_filesystem is True
    synced = definition.application_config(_bindings(synced_source=True))
    assert synced.granian_config is not None
    assert synced.granian_config.reload is True
    assert synced.container_security_context is not None
    assert synced.container_security_context.read_only_root_filesystem is False


def test_extra_volumes_follow_the_tmp_emptydir():
    config = definition.application_config(_bindings())
    assert [v.name for v in config.extra_volumes] == ["tmp"]


def test_the_canonical_host_needs_its_host_and_vault():
    with pytest.raises(ValueError, match=r"hostnames\.canonical"):
        definition.canonical_oidc_config(
            _bindings(
                hostnames=definition.OlAnalyticsApiHostnames(
                    canonical=None, learn="analytics.learn.mit.test"
                )
            )
        )
    with pytest.raises(ValueError, match="Vault"):
        definition.canonical_oidc_config(
            _bindings(
                cluster=definition.ClusterCapabilities(),
                oidc_secret_name=OIDC_SECRET,
            )
        )


def test_the_learn_host_reads_mit_learns_client_from_vault_or_a_secret():
    deployed = definition.learn_oidc_config(_bindings())
    assert deployed.credentials == OLApisixOIDCVaultCredentials(
        path=LearnSession.vault_path, vaultauth="vaultauth"
    )
    local = definition.learn_oidc_config(
        _bindings(
            env_suffix="dev",
            hostnames=definition.OlAnalyticsApiHostnames(
                canonical=None, learn="analytics.learn.mit.test"
            ),
            cluster=definition.ClusterCapabilities(registry="direct"),
            oidc_secret_name=OIDC_SECRET,
        )
    )
    assert local.credentials == OLApisixOIDCSecretCredentials(secret_name=OIDC_SECRET)
    assert local.oidc_session_cookie_domain == ".learn.mit.test"


def test_cors_origins_lead_with_the_canonical_host_when_there_is_one():
    assert definition.cors_allow_origins(_bindings()) == [
        "https://analytics-qa.ol.mit.edu",
        "https://rc.learn.mit.edu",
    ]
    local = _bindings(
        hostnames=definition.OlAnalyticsApiHostnames(
            canonical=None, learn="analytics.learn.mit.test"
        )
    )
    assert definition.cors_allow_origins(local) == ["https://rc.learn.mit.edu"]
    assert definition.hosts(local) == ["analytics.learn.mit.test"]


def test_the_templated_secrets_render_the_deployed_text():
    secrets = vault_secrets.vault_secrets()
    oauth = vault_secrets.secret_named(secrets, "ol-analytics-api-mitxonline-oauth")
    assert oauth.templates == {
        vault_secrets.MITXONLINE_CLIENT_ID: '{{ get .Secrets "client_id" }}',
        vault_secrets.MITXONLINE_CLIENT_SECRET: '{{ get .Secrets "client_secret" }}',
    }
    assert vault_secrets.vault_keys(oauth) == ["client_id", "client_secret"]
    records = vault_secrets.secret_named(
        secrets, "ol-analytics-api-learner-records-oidc"
    )
    assert vault_secrets.vault_keys(records) == ["url", "client_id"]


def test_secret_env_names_lists_every_described_variable():
    assert vault_secrets.secret_env_names() == [
        "SENTRY_DSN",
        "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_ID",
        "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_SECRET",
        "OL_ANALYTICS_API_B2B_LEARNER_RECORDS_ISSUER",
        "OL_ANALYTICS_API_B2B_LEARNER_RECORDS_AUDIENCE",
    ]


def test_vault_keys_refuses_template_text():

    secret = VaultSecret(
        name="x",
        kind="static",
        mount="m",
        mount_type="kv-v1",
        path="p",
        env={"A": Template("{{ .Secrets }}")},
    )
    with pytest.raises(ValueError, match="template text"):
        vault_secrets.vault_keys(secret)
