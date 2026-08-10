"""Per-Pulumi-project mapping of the SOPS secrets each project actually reads.

Concourse ``git`` resources trigger on ``paths``.  Watching the whole of
``src/bridge/secrets/`` -- which is what ``PULUMI_WATCHED_PATHS`` used to do --
means editing one application's secret file re-triggers *every* Pulumi pipeline
in both Concourse instances.  This module narrows that: a pipeline watches only
the secrets directories whose contents its own Pulumi project decrypts.

The mapping keys are Pulumi project paths relative to ``src/ol_infrastructure/``
(the same value pipelines pass as ``pulumi_project_path`` /
``project_source_path``).  Values are entries under ``src/bridge/secrets/`` --
either a directory name or a bare file name.

The mapping is derived from static analysis of ``read_yaml_secrets`` /
``read_json_secrets`` / ``set_env_secrets`` calls, following imports so that
secrets read through shared modules (``lib/fastly.py``, ``lib/heroku.py``,
``lib/consul.py``, ``substructure/aws/eks/grafana.py``, ...) are attributed to
every project that imports them.  ``tests/ol_concourse/test_secrets_map.py``
re-runs that analysis and fails if the two ever disagree, so this file cannot
silently rot.
"""

SECRETS_ROOT = "src/bridge/secrets"  # pragma: allowlist secret

# Secrets that only ever supply *deploy-time provider credentials* and never end
# up in any managed resource's inputs.  Rotating one of these changes how Pulumi
# authenticates on the next run; it does not change the desired state of
# anything, so it must not trigger a deploy.  ``pulumi/vault.*.yaml`` is the
# reason this exemption exists at all -- ``lib.vault.get_vault_provider`` reads
# it, and nearly every project imports ``lib.vault``, so watching it would
# re-create the exact cascade this module exists to remove.
#
# Anything NOT listed here is treated as content-bearing and must be watched by
# each project that reads it.  Note that some files are provider credentials for
# most readers but content for one -- e.g. ``fastly.yaml`` is provider auth via
# ``lib/fastly.py`` but ``infrastructure/vector_log_proxy`` writes its
# ``global_read_api_key`` into Vault -- so those stay watched.
# Keys are secret paths relative to ``src/bridge/secrets/`` as rendered by the
# static analysis in ``tests/ol_concourse/test_secrets_map.py`` (``*`` stands in
# for an f-string hole); values explain why the read is credential-only.
DEPLOY_CREDENTIAL_SECRETS: dict[str, str] = {
    "pulumi/vault.*.yaml": "lib.vault.get_vault_provider -- Vault provider auth",
    "pulumi/vault.*.*.yaml": "infrastructure/vault -- Vault provider auth",
    "pulumi/consul.*.yaml": "lib.consul.get_consul_provider -- Consul provider auth",
    "pulumi/github_app.yaml": "lib.github_helper -- GitHub provider auth",
    "pulumi/mongodb_atlas.yaml": "MongoDB Atlas provider public/private key pair",
}

# (project, secret) pairs where a normally credential-only secret really is
# content for that one project, so its pipeline must still watch it.
CONTENT_BEARING_OVERRIDES = frozenset(
    {
        # basic_auth_password is baked into the Consul server cloud-init userdata.
        ("infrastructure/consul/", "pulumi/consul.*.yaml"),
    }
)

# Reads whose directory component is computed at runtime and so cannot be
# resolved statically.  Recording what each one actually resolves to keeps the
# drift test meaningful instead of silently skipping the read.
DYNAMIC_SECRET_READS: dict[str, dict[str, list[str]]] = {
    # "<env_prefix>/secrets.<env>.yaml" for the OpenSearch OpenAI connector,
    # which is only enabled for the mitlearn deployment group today.
    "infrastructure/aws/opensearch/": {"*/secrets.*.yaml": ["mitlearn/"]},
}

# Pulumi project path (relative to src/ol_infrastructure/) -> secrets it reads.
# Projects that read no secrets are present with an empty list so that the drift
# test can tell "audited, reads nothing" apart from "never audited".
PROJECT_SECRETS: dict[str, list[str]] = {
    # ---- applications/ ----------------------------------------------------
    "applications/airbyte/": ["airbyte/"],
    "applications/b2b_partners_storage/": [],
    "applications/celery_monitoring/": ["heroku/"],
    "applications/clickhouse/": [],
    "applications/codejail/": [],
    "applications/concourse/": ["concourse/", "vector/"],
    "applications/dagster/": [],
    "applications/digital_credentials/": ["digital_credentials/"],
    "applications/ecs_test/": [],
    "applications/edx_notes/": ["edx_notes/"],
    "applications/edxapp/": [
        "edxapp/",
        "fastly.yaml",
        # mongodb_atlas.<deployment>.<env>.yaml holds the Atlas DB user
        # passwords edxapp manages; the bare mongodb_atlas.yaml next to it is
        # provider auth only, so it is deliberately not matched.
        "pulumi/mongodb_atlas.*.*.yaml",
        "vector/",
    ],
    "applications/fastly_redirector/": ["fastly.yaml"],
    "applications/google_ads_optimization/": [],
    "applications/gwarek/": [],
    "applications/jupyterhub/": [],
    "applications/jupyterhub_data/": ["jupyterhub_data/"],
    "applications/keycloak/": ["keycloak/"],
    "applications/kubewatch/": ["kubewatch/"],
    "applications/kubewatch_webhook_handler/": ["kubewatch/"],
    "applications/learn_ai/": [
        "fastly.yaml",
        "learn_ai/",
        "mitopen/",
        "vault/",
        "vector/",
    ],
    "applications/mailgun/": [],
    "applications/marimo_data/": [],
    "applications/micromasters/": ["fastly.yaml", "vector/"],
    "applications/mit_learn/": [
        "fastly.yaml",
        "mitlearn/",
        "qdrant_cloud/",
        "vector/",
    ],
    "applications/mit_learn_nextjs/": [],
    "applications/mitxonline/": ["fastly.yaml", "mitxonline/"],
    "applications/ocw_site/": ["fastly.yaml", "vector/"],
    "applications/ocw_studio/": ["ocw_studio/"],
    "applications/odl_video_service/": ["odl_video_service/"],
    "applications/ol_analytics_api/": [],
    "applications/omnigraph/": ["omnigraph/"],
    "applications/open_discussions/": ["heroku/"],
    "applications/open_metadata/": ["open_metadata/"],
    "applications/opik/": [],
    "applications/release_bot/": ["concourse/", "release_bot/"],
    "applications/starburst/": [],
    "applications/starrocks/": [],
    "applications/superset/": ["superset/"],
    "applications/tika/": ["tika/"],
    "applications/toolhive_apps/": [],
    "applications/toolhive_data/": [],
    "applications/toolhive_operator/": [],
    "applications/toolhive_swe/": [],
    "applications/witan/": ["witan/"],
    "applications/xpro/": ["fastly.yaml", "vector/", "xpro/"],
    "applications/xqueue/": [],
    "applications/xqwatcher/": [],
    # ---- infrastructure/ --------------------------------------------------
    "infrastructure/aws/data_warehouse/": [],
    "infrastructure/aws/dns/": [],
    "infrastructure/aws/ecr/": [],
    "infrastructure/aws/eks/": ["fastly.yaml"],
    "infrastructure/aws/iam/": [],
    "infrastructure/aws/kms/": [],
    "infrastructure/aws/network/": [],
    # opensearch also reads "<env_prefix>/secrets.<env>.yaml" for the OpenAI
    # connector; today the only env_prefix that enables it is mitlearn.
    "infrastructure/aws/opensearch/": ["mitlearn/", "opensearch/"],
    "infrastructure/aws/policies/": [],
    "infrastructure/aws/private_ca/": [],
    "infrastructure/aws/s3_sites/": [],
    "infrastructure/aws/sftp_servers/": [],
    # azure.<env>.yaml is provider auth for this project, but its subscription_id
    # and tenant_id are also exported as stack outputs and end up in the Vault
    # backend configuration, so it is content-bearing rather than credential-only.
    "infrastructure/azure/openai/": ["pulumi/azure.*.yaml"],
    # consul.<env>.yaml's basic_auth_password is baked into the server cloud-init
    # userdata, so it is content-bearing here (unlike the provider-auth reads in
    # substructure/consul and applications/concourse via lib.consul).
    "infrastructure/consul/": ["pulumi/consul.*.yaml", "vector/"],
    "infrastructure/grafana_alerting/": ["grafana_cloud/"],
    "infrastructure/grafana_cloud/": [],
    "infrastructure/mongodb_atlas/": [],
    "infrastructure/monitoring/": [],
    "infrastructure/qdrant_cloud/": ["qdrant_cloud/"],
    "infrastructure/sentry/": ["sentry/"],
    "infrastructure/vault/": ["vector/"],
    "infrastructure/vector_log_proxy/": ["fastly.yaml", "vector/"],
    # ---- saas/ ------------------------------------------------------------
    "saas/github/organization/": [],
    "saas/github/repositories/": [],
    "saas/rootly/": ["rootly/"],
    # ---- substructure/ ----------------------------------------------------
    "substructure/aws/eks/": ["alloy/"],
    "substructure/consul/": [],
    "substructure/keycloak/": [],
    "substructure/open_metadata/": [],
    "substructure/starrocks/": [],
    "substructure/vault/auth/": [],
    "substructure/vault/azure/": [],
    "substructure/vault/encryption_mounts/": [],
    "substructure/vault/pki/": [],
    "substructure/vault/secrets/": ["alloy/", "mitopen/", "vault/"],
    "substructure/vault/setup/": [],
    "substructure/vault/static_mounts/": [],
    "substructure/xpro_partner_dns/": [],
}


def secrets_paths(*entries: str) -> list[str]:
    """Return ``src/bridge/secrets`` watched-path entries for ``entries``.

    :param entries: Directory names (``"edxapp/"``) or bare file names
        (``"fastly.yaml"``) beneath ``src/bridge/secrets/``.
    :returns: Full repo-relative paths suitable for a git resource's ``paths``.
    """
    return [f"{SECRETS_ROOT}/{entry}" for entry in entries]


def project_secrets_paths(project_path: str) -> list[str]:
    """Return the watched secrets paths for a Pulumi project.

    :param project_path: Project path relative to ``src/ol_infrastructure/``,
        e.g. ``"applications/edxapp/"``.  A missing trailing slash is tolerated.
    :returns: Repo-relative paths under ``src/bridge/secrets/``.  Empty for
        projects that read no secrets.
    :raises KeyError: If the project has never been audited -- add it to
        :data:`PROJECT_SECRETS` (an empty list is a valid answer).
    """
    key = project_path if project_path.endswith("/") else f"{project_path}/"
    try:
        entries = PROJECT_SECRETS[key]
    except KeyError:
        msg = (
            f"Pulumi project {key!r} is missing from PROJECT_SECRETS in "
            "src/ol_concourse/pipelines/secrets_map.py. Add it (an empty list "
            "is correct for projects that read no SOPS secrets) so its pipeline "
            "watches the right secrets."
        )
        raise KeyError(msg) from None
    return secrets_paths(*entries)


def combined_secrets_paths(*project_paths: str) -> list[str]:
    """Return the deduplicated union of several projects' watched secrets paths.

    Use this when one git resource feeds Pulumi jobs for more than one project
    (e.g. the Vault pipeline's substructure resource, which drives every
    ``substructure/vault/*`` project from a single checkout).

    :param project_paths: Project paths relative to ``src/ol_infrastructure/``.
    :returns: Sorted, deduplicated repo-relative paths under
        ``src/bridge/secrets/``.
    """
    return sorted({p for path in project_paths for p in project_secrets_paths(path)})
