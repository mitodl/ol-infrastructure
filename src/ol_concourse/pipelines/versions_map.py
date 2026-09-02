"""Per-project mapping of the version pins each build actually reads.

Concourse ``git`` resources trigger on ``paths``, and ``paths`` are *files*.
``src/bridge/lib/versions.py`` is one file holding every version this
infrastructure runs, so a pipeline that cares about a single constant in it --
the Keycloak pipeline and ``KEYCLOAK_VERSION``, the Concourse image pipeline and
``CONCOURSE_VERSION`` -- had to watch the whole file.  Bumping any unrelated
Helm chart there re-triggered those pipelines' AMI/image builds and rolled them
all the way through CI -> QA -> Production.  This is the same failure the
sibling :mod:`ol_concourse.pipelines.secrets_map` exists to fix for SOPS
secrets, and it is fixed the same way.

``versions.py`` stays the single readable record.  ``sync_version_pins.py``
projects it into ``src/bridge/lib/version_pins/``, one extensionless file per
constant, and this module records which of those files each Pulumi project and
Packer image needs.  A pipeline then watches only its own pins.

The mapping is derived from static analysis of ``from bridge.lib.versions
import ...``, following local imports so that constants reached through shared
modules (``lib/aws/eks_helper.py``, ``components/services/...``) are attributed
to every project that imports them.  ``tests/ol_concourse/test_versions_map.py``
re-runs that analysis and fails if the two ever disagree, so this file cannot
silently rot.

Projects and images that read no version constant are present with an empty
list, so the drift test can tell "audited, reads nothing" apart from "never
audited".  An empty list is the *right* answer for most of them, and it means
their pipeline no longer re-triggers on ``versions.py`` at all.
"""

VERSION_PINS_ROOT = "src/bridge/lib/version_pins"

# Pulumi project path (relative to src/ol_infrastructure/) -> version constants
# it reads, directly or through a module it imports.
PROJECT_VERSIONS: dict[str, list[str]] = {
    # ---- applications/ ------------------------------------------------------
    "applications/airbyte/": ["AIRBYTE_CHART_VERSION"],
    "applications/b2b_partners_storage/": [],
    "applications/celery_monitoring/": ["LEEK_VERSION"],
    "applications/clickhouse/": [
        "CLICKHOUSE_KEEPER_VERSION",
        "CLICKHOUSE_OPERATOR_VERSION",
        "CLICKHOUSE_SERVER_VERSION",
    ],
    "applications/codejail/": [],
    "applications/concourse/": [],
    "applications/dagster/": [
        "DAGSTER_CHART_VERSION",
        "PGBOUNCER_EXPORTER_VERSION",
        "PGBOUNCER_VERSION",
        "SQL_EXPORTER_VERSION",
    ],
    "applications/digital_credentials/": [],
    "applications/ecs_test/": [],
    "applications/edx_notes/": ["NGINX_VERSION"],
    "applications/edxapp/": [
        "MEILISEARCH_CHART_VERSION",
        "MEILISEARCH_VERSION",
        "NGINX_VERSION",
        "TYPESENSE_VERSION",
    ],
    "applications/fastly_redirector/": [],
    "applications/google_ads_optimization/": [],
    "applications/gwarek/": [],
    "applications/jupyterhub/": ["JUPYTERHUB_CHART_VERSION"],
    "applications/jupyterhub_data/": [
        "JUPYTERHUB_CHART_VERSION",
        "MARIMO_JUPYTERLAB_VERSION",
    ],
    "applications/keycloak/": ["KEYCLOAK_OPERATOR_CRD_VERSION"],
    "applications/kubewatch/": ["KUBEWATCH_CHART_VERSION"],
    "applications/kubewatch_webhook_handler/": [],
    "applications/learn_ai/": ["NGINX_VERSION"],
    "applications/mailgun/": [],
    "applications/marimo_data/": [],
    "applications/micromasters/": ["NGINX_VERSION"],
    "applications/mit_learn/": ["NGINX_VERSION"],
    "applications/mit_learn_nextjs/": [],
    "applications/mitxonline/": ["NGINX_VERSION"],
    "applications/ocw_site/": [],
    "applications/ocw_studio/": ["NGINX_VERSION"],
    "applications/odl_video_service/": ["NGINX_VERSION"],
    "applications/ol_analytics_api/": ["NGINX_VERSION"],
    "applications/omnigraph/": [],
    "applications/open_discussions/": [],
    "applications/open_metadata/": ["OPEN_METADATA_VERSION"],
    "applications/opik/": ["OPIK_CHART_VERSION"],
    "applications/release_bot/": [],
    "applications/starburst/": [],
    "applications/starrocks/": [
        "STARROCKS_CHART_VERSION",
        "STARROCKS_VERSION",
    ],
    "applications/superset/": ["SUPERSET_CHART_VERSION"],
    "applications/tika/": ["TIKA_CHART_VERSION"],
    "applications/toolhive_apps/": [],
    "applications/toolhive_data/": [],
    "applications/toolhive_operator/": [
        "TOOLHIVE_OPERATOR_CHART_VERSION",
        "TOOLHIVE_OPERATOR_CRDS_CHART_VERSION",
    ],
    "applications/toolhive_swe/": [
        "MCP_CONTEXT7_VERSION",
        "MCP_GRAFANA_VERSION",
        "MCP_PROXY_FOR_AWS_VERSION",
        "MCP_SENTRY_VERSION",
    ],
    "applications/vuln_scanner/": [],
    "applications/witan/": [],
    "applications/xpro/": ["NGINX_VERSION"],
    "applications/xqueue/": ["NGINX_VERSION"],
    "applications/xqwatcher/": [],
    # ---- infrastructure/ ----------------------------------------------------
    "infrastructure/aws/data_warehouse/": [],
    # Empty deliberately: the Azure OpenAI project pins no version constants.
    # Model versions are stack config (azure_openai:model_versions), not
    # src/bridge/lib/versions.py entries, because which versions exist is a
    # property of the subscription and region rather than of this repo.
    "infrastructure/azure/openai/": [],
    "infrastructure/aws/dns/": [],
    "infrastructure/aws/ecr/": [],
    "infrastructure/aws/eks/": [
        "APISIX_CHART_VERSION",
        "AWS_LOAD_BALANCER_CONTROLLER_CHART_VERSION",
        "AWS_NODE_TERMINATION_HANDLER_CHART_VERSION",
        "CERT_MANAGER_CHART_VERSION",
        "EBS_CSI_DRIVER_VERSION",
        "EFS_CSI_DRIVER_VERSION",
        "EXTERNAL_DNS_CHART_VERSION",
        "GATEWAY_API_VERSION",
        "PROMETHEUS_OPERATOR_CRD_VERSION",
        "TRAEFIK_CHART_VERSION",
        "VAULT_SECRETS_OPERATOR_CHART_VERSION",
        "VPA_CHART_VERSION",
    ],
    "infrastructure/aws/iam/": [],
    "infrastructure/aws/kms/": [],
    "infrastructure/aws/network/": [],
    "infrastructure/aws/opensearch/": [],
    "infrastructure/aws/policies/": [],
    "infrastructure/aws/private_ca/": [],
    "infrastructure/aws/s3_sites/": [],
    "infrastructure/aws/sftp_servers/": [],
    "infrastructure/consul/": [],
    "infrastructure/gcp/": [],
    "infrastructure/grafana_alerting/": [],
    "infrastructure/grafana_cloud/": [],
    "infrastructure/mongodb_atlas/": [],
    "infrastructure/monitoring/": [],
    "infrastructure/qdrant_cloud/": ["QDRANT_VERSION"],
    "infrastructure/sentry/": [],
    "infrastructure/vault/": [],
    "infrastructure/vector_log_proxy/": ["VECTOR_VERSION"],
    # ---- saas/ --------------------------------------------------------------
    "saas/github/organization/": [],
    "saas/github/repositories/": [],
    "saas/rootly/": [],
    # ---- substructure/ ------------------------------------------------------
    "substructure/aws/eks/": [
        "CLICKHOUSE_OPERATOR_VERSION",
        "GRAFANA_K8S_MONITORING_CHART_VERSION",
        "KARPENTER_CHART_VERSION",
        "KEDA_CHART_VERSION",
        "LOCAL_PATH_PROVISIONER_CHART_VERSION",
        "MARIMO_OPERATOR_VERSION",
        "NVIDIA_DCGM_EXPORTER_CHART_VERSION",
        "NVIDIA_K8S_DEVICE_PLUGIN_CHART_VERSION",
        "STARROCKS_OPERATOR_CHART_VERSION",
        "TYPESENSE_OPERATOR_CHART_VERSION",
        "VANTAGE_K8S_AGENT_CHART_VERSION",
    ],
    "substructure/consul/": [],
    "substructure/keycloak/": [],
    "substructure/open_metadata/": ["OPEN_METADATA_VERSION"],
    # Only the checksum is read here -- the plugin URL is built from the release
    # asset name.  The two always move together in versions.py, so watching the
    # checksum is enough to catch a plugin upgrade.
    "substructure/starrocks/": ["VAULT_PLUGIN_STARROCKS_SHA256"],
    "substructure/vault/auth/": [],
    "substructure/vault/encryption_mounts/": [],
    "substructure/vault/pki/": [],
    "substructure/vault/secrets/": [],
    "substructure/vault/setup/": [],
    "substructure/vault/static_mounts/": [],
    "substructure/xpro_partner_dns/": [],
}

# Packer image directory name (under src/bilder/images/) -> version constants
# baked into that AMI.
IMAGE_VERSIONS: dict[str, list[str]] = {
    "codejail": [
        "CONSUL_VERSION",
        "VAULT_VERSION",
    ],
    "concourse": [
        "CONCOURSE_VERSION",
        "CONSUL_VERSION",
        "TRAEFIK_VERSION",
        "VAULT_VERSION",
    ],
    "consul": [
        "CONSUL_VERSION",
        "TRAEFIK_VERSION",
    ],
    "docker_baseline_ami": [
        "CONSUL_TEMPLATE_VERSION",
        "CONSUL_VERSION",
        "VAULT_VERSION",
    ],
    "edxapp_v2": [],
    "vault": [
        "CONSUL_VERSION",
        "TRAEFIK_VERSION",
        "VAULT_PLUGIN_STARROCKS_SHA256",
        "VAULT_PLUGIN_STARROCKS_VERSION",
        "VAULT_VERSION",
    ],
}


def version_pin_paths(*names: str) -> list[str]:
    """Return watched-path entries for the named version constants.

    :param names: Constant names as they appear in ``bridge.lib.versions``,
        e.g. ``"CONCOURSE_VERSION"``.
    :returns: Repo-relative paths under ``src/bridge/lib/version_pins/``,
        suitable for a git resource's ``paths``.
    """
    return [f"{VERSION_PINS_ROOT}/{name}" for name in sorted(set(names))]


def project_version_paths(project_path: str) -> list[str]:
    """Return the watched version-pin paths for a Pulumi project.

    :param project_path: Project path relative to ``src/ol_infrastructure/``,
        e.g. ``"applications/keycloak/"``.  A missing trailing slash is
        tolerated.
    :returns: Repo-relative paths under ``src/bridge/lib/version_pins/``.
        Empty for projects that read no version constants, which is the point:
        their pipeline stops re-triggering on unrelated version bumps.
    :raises KeyError: If the project has never been audited -- add it to
        :data:`PROJECT_VERSIONS` (an empty list is a valid answer).  Failing
        loudly at render time beats generating a pipeline that quietly watches
        nothing and stops deploying.
    """
    key = project_path if project_path.endswith("/") else f"{project_path}/"
    try:
        names = PROJECT_VERSIONS[key]
    except KeyError:
        msg = (
            f"Pulumi project {key!r} is missing from PROJECT_VERSIONS in "
            "src/ol_concourse/pipelines/versions_map.py. Add it (an empty list "
            "is correct for projects that read no version constants) so its "
            "pipeline watches the right version pins."
        )
        raise KeyError(msg) from None
    return version_pin_paths(*names)


def combined_version_paths(*project_paths: str) -> list[str]:
    """Return the union of the version pins several Pulumi projects read.

    For the pipelines where one git resource drives a whole family of projects
    from a single checkout -- ``substructure/vault/*`` -- so the resource has to
    watch everything any member of the family reads.

    :param project_paths: Project paths relative to ``src/ol_infrastructure/``.
    :returns: Deduplicated repo-relative paths under
        ``src/bridge/lib/version_pins/``.
    """
    return sorted({path for p in project_paths for path in project_version_paths(p)})


def image_version_paths(image_name: str) -> list[str]:
    """Return the watched version-pin paths for a Packer image.

    :param image_name: Directory name under ``src/bilder/images/``, e.g.
        ``"docker_baseline_ami"``.
    :returns: Repo-relative paths under ``src/bridge/lib/version_pins/``.
    :raises KeyError: If the image has never been audited -- add it to
        :data:`IMAGE_VERSIONS` (an empty list is a valid answer).
    """
    try:
        names = IMAGE_VERSIONS[image_name]
    except KeyError:
        msg = (
            f"Packer image {image_name!r} is missing from IMAGE_VERSIONS in "
            "src/ol_concourse/pipelines/versions_map.py. Add it (an empty list "
            "is correct for images that bake in no pinned version) so its AMI "
            "rebuilds when a version it installs changes."
        )
        raise KeyError(msg) from None
    return version_pin_paths(*names)
