"""CloudNativePG operator and shared PostgreSQL cluster for local-dev."""

from collections.abc import Callable
from dataclasses import dataclass

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


@dataclass
class DatabaseResources:
    cnpg_release: k8s.helm.v3.Release
    credentials: k8s.core.v1.Secret
    cluster: k8s.apiextensions.CustomResource
    """The CNPG Cluster CR — pass as a dependency to workloads that need Postgres."""


def create_database(
    _k8s: Callable[..., ResourceOptions],
    local_infra_ns: k8s.core.v1.Namespace,
    cnpg_version: str,
) -> DatabaseResources:
    """Deploy the CloudNativePG operator and a shared PostgreSQL cluster.

    A single CNPG cluster hosts all app databases.  A fixed-password credential
    Secret is used so app DATABASE_URLs are predictable and don't require reading
    a CNPG-generated random password at deploy time.
    """
    cnpg_release = k8s.helm.v3.Release(
        "cnpg-operator",
        k8s.helm.v3.ReleaseArgs(
            name="cnpg",
            chart="cloudnative-pg",
            version=cnpg_version,
            namespace="local-infra",
            repository_opts=k8s.helm.v3.RepositoryOptsArgs(
                repo="https://cloudnative-pg.github.io/charts",
            ),
            cleanup_on_fail=True,
            timeout=600,
            values={
                "replicaCount": 1,
                "resources": {
                    "limits": {"memory": "256Mi"},
                },
            },
        ),
        opts=_k8s(parent=local_infra_ns),
    )

    credentials = k8s.core.v1.Secret(
        "pg-credentials",
        metadata={"name": "pg-app-credentials", "namespace": "local-infra"},
        string_data={
            "username": "app",
            "password": "localdev",  # pragma: allowlist secret
        },
        opts=_k8s(parent=local_infra_ns),
    )

    cluster = k8s.apiextensions.CustomResource(
        "local-pg-cluster",
        api_version="postgresql.cnpg.io/v1",
        kind="Cluster",
        metadata={
            "name": "local-pg",
            "namespace": "local-infra",
        },
        spec={
            "instances": 1,
            "storage": {"size": "10Gi"},
            "bootstrap": {
                "initdb": {
                    "database": "app",
                    "owner": "app",
                    "secret": {"name": "pg-app-credentials"},
                    "postInitSQL": [
                        "CREATE DATABASE mitlearn OWNER app;",
                        "CREATE DATABASE learnai OWNER app;",
                        "CREATE DATABASE mitxonline OWNER app;",
                        "CREATE DATABASE odlvideo OWNER app;",
                        "CREATE DATABASE keycloak OWNER app;",
                        "CREATE DATABASE litellm OWNER app;",
                    ],
                }
            },
            "postgresql": {
                "parameters": {"max_connections": "100"},
            },
            "resources": {
                "limits": {"memory": "512Mi"},
            },
        },
        opts=_k8s(parent=local_infra_ns, depends_on=[cnpg_release, credentials]),
    )

    return DatabaseResources(
        cnpg_release=cnpg_release,
        credentials=credentials,
        cluster=cluster,
    )
