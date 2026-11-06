"""Self-hosted telemetry archive on the operations EKS cluster (ADR-0014).

Phase 0 built what Loki and Mimir share: the S3 buckets and IRSA roles, the
internal write endpoint, and the Private Data Source Connect agent through which
Grafana Cloud reads the archive. Phase 1 added Loki, its push routes and its
Cloud datasource. Phase 2 adds Mimir, its push routes, the recording rules its
ruler evaluates, and its Cloud datasource. See
docs/plans/telemetry-archive-implementation-plan.md.
"""

import json
from pathlib import Path

import pulumi_vault as vault
from pulumi import Config, ResourceOptions, export, log
from pulumi_aws import get_caller_identity

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.observability_archive import (
    datasources,
    ingress,
    loki,
    mimir,
    pdc,
    recording_rules,
    storage,
)
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import make_stack_reference, parse_stack
from ol_infrastructure.lib.telemetry_archive import (
    ARCHIVE_DOMAINS,
    ARCHIVE_NAMESPACE,
    LOKI_PUSH_PATH,
    OTLP_LOGS_PATH,
    OTLP_METRICS_PATH,
    PROMETHEUS_PUSH_PATH,
)
from ol_infrastructure.lib.vault import setup_vault_provider

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()

stack_info = parse_stack()
archive_config = Config("observability_archive")
network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")

NAMESPACE = ARCHIVE_NAMESPACE
APPLICATION_NAME = "observability-archive"
VAULT_MOUNT_PATH = "secret-observability-archive"
PUSH_AUTH_SECRET_NAME = "telemetry-archive-push-auth"  # noqa: S105  # pragma: allowlist secret

# Shared with every writer through lib/telemetry_archive.py, so the endpoint
# the collectors ship to is the one this stack builds.
archive_domain = ARCHIVE_DOMAINS[stack_info.env_suffix]
retention_days = archive_config.require_int("retention_days")

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": f"operations-{stack_info.env_suffix}",
    }
)
aws_account = get_caller_identity()
k8s_labels = K8sGlobalLabels(
    service=Services.telemetry_archive,
    ou=BusinessUnit.operations,
    stack=stack_info,
)
k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.k8s_name,
}

# Every archive workload is pinned to the archive-xlarge node group (D4).
ARCHIVE_NODE_SELECTOR = {"ol.mit.edu/archive_node": "true"}
ARCHIVE_TOLERATIONS = [
    {
        "key": "ol.mit.edu/archive_workload",
        "operator": "Equal",
        "value": "true",
        "effect": "NoSchedule",
    }
]

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))


def require_shared_traefik_selector(selector: str | None) -> str:
    """Refuse to deploy before the shared Traefik filters GatewayClasses.

    The archive runs its own Traefik on the same Gateway API controller name as
    the cluster's public one. Until the EKS stack has deployed the shared Traefik
    with its GatewayClass label selector, that public controller would also
    serve the archive Gateway on its internet-facing NLB. The ingress depends on
    this check, so nothing it creates can exist before the check has passed.
    """
    if not selector:
        msg = (
            "The operations EKS stack does not export traefik_gateway_class_selector. "
            "Apply infrastructure/aws/eks first so the shared Traefik stops serving "
            "other GatewayClasses, then deploy this stack."
        )
        raise RuntimeError(msg)
    return selector


shared_gateway_class_selector = cluster_stack.get_output(
    "traefik_gateway_class_selector"
).apply(require_shared_traefik_selector)

##################################
#   Vault: push credentials      #
##################################

archive_secrets_mount = vault.Mount(
    "observability-archive-secrets",
    description="Secrets for the telemetry archive (ADR-0014)",
    path=VAULT_MOUNT_PATH,
    type="kv-v2",
)
push_credentials = read_yaml_secrets(
    Path(f"observability_archive/archive.{stack_info.env_suffix}.yaml")
)
vault.generic.Secret(
    "observability-archive-push-credentials",
    path=archive_secrets_mount.path.apply(lambda p: f"{p}/push_credentials"),
    data_json=json.dumps(
        {
            "username": push_credentials["push_username"],
            "password": push_credentials["push_password"],
            # The htpasswd line Traefik's basicAuth middleware consumes. Stored
            # rather than derived so the synced Secret does not churn on every
            # refresh with a fresh bcrypt salt.
            "htpasswd": push_credentials["push_htpasswd"],
        }
    ),
)

# Vault Kubernetes auth and the Secrets Operator wiring. The IRSA role this also
# creates is unused: each backend gets its own in storage.py.
archive_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name=APPLICATION_NAME,
        namespace=NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath(
            "observability_archive_policy.hcl"
        ),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name=APPLICATION_NAME,
        vault_sync_service_account_names=f"{APPLICATION_NAME}-vault",
        k8s_labels=k8s_labels,
    )
)

push_auth_secret = OLVaultK8SSecret(
    f"telemetry-archive-push-auth-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="telemetry-archive-push-auth",
        namespace=NAMESPACE,
        labels=k8s_labels.model_dump(),
        dest_secret_labels=k8s_labels.model_dump(),
        dest_secret_name=PUSH_AUTH_SECRET_NAME,
        mount=VAULT_MOUNT_PATH,
        mount_type="kv-v2",
        path="push_credentials",
        templates={"users": "{{ .Secrets.htpasswd }}"},
        refresh_after="1h",
        vaultauth=archive_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[archive_auth_binding.vault_k8s_resources.vso_resources],
    ),
)

##################################
#      Storage + write path      #
##################################

archive_storage = storage.build(
    stack_info=stack_info,
    aws_config=aws_config,
    retention_days=retention_days,
    namespace=NAMESPACE,
    account_id=aws_account.account_id,
    cluster_name=cluster_stack.require_output("cluster_name"),
    cluster_identities=cluster_stack.require_output("cluster_identities"),
)

archive_ingress = ingress.build(
    stack_info=stack_info,
    aws_config=aws_config,
    k8s_labels=k8s_labels,
    k8s_global_labels=k8s_global_labels,
    namespace=NAMESPACE,
    domain=archive_domain,
    network_stack=network_stack,
    node_selector=ARCHIVE_NODE_SELECTOR,
    tolerations=ARCHIVE_TOLERATIONS,
    push_auth_secret_name=PUSH_AUTH_SECRET_NAME,
    depends_on=shared_gateway_class_selector.apply(lambda _: [push_auth_secret]),
)

##################################
#        Loki (Phase 1)          #
##################################

loki_storage = archive_storage["loki"]
loki_release = loki.build(
    stack_info=stack_info,
    aws_config=aws_config,
    k8s_global_labels=k8s_global_labels,
    namespace=NAMESPACE,
    cluster_name=cluster_stack.require_output("cluster_name"),
    bucket_name=loki_storage.bucket_name,
    irsa_role_arn=loki_storage.irsa_role.role.arn,
    service_account=loki_storage.service_account,
    retention_days=retention_days,
    node_selector=ARCHIVE_NODE_SELECTOR,
    tolerations=ARCHIVE_TOLERATIONS,
    depends_on=[loki_storage.bucket, loki_storage.irsa_role],
)
ingress.push_route(
    name="loki-push",
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_labels=k8s_labels,
    ingress=archive_ingress,
    paths=[LOKI_PUSH_PATH, OTLP_LOGS_PATH],
    backend_service=loki_release.distributor_service,
    backend_port=loki_release.http_port,
    depends_on=[loki_release.release],
)

##################################
#        Mimir (Phase 2)         #
##################################

mimir_storage = archive_storage["mimir"]
mimir_release = mimir.build(
    stack_info=stack_info,
    aws_config=aws_config,
    k8s_global_labels=k8s_global_labels,
    namespace=NAMESPACE,
    cluster_name=cluster_stack.require_output("cluster_name"),
    bucket_name=mimir_storage.bucket_name,
    irsa_role_arn=mimir_storage.irsa_role.role.arn,
    service_account=mimir_storage.service_account,
    retention_days=retention_days,
    node_selector=ARCHIVE_NODE_SELECTOR,
    tolerations=ARCHIVE_TOLERATIONS,
    depends_on=[mimir_storage.bucket, mimir_storage.irsa_role],
)
ingress.push_route(
    name="mimir-push",
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_labels=k8s_labels,
    ingress=archive_ingress,
    paths=[PROMETHEUS_PUSH_PATH, OTLP_METRICS_PATH],
    backend_service=mimir_release.distributor_service,
    backend_port=mimir_release.http_port,
    depends_on=[mimir_release.release],
)
recording_rules.build(
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_labels=k8s_labels,
    depends_on=[mimir_release.release],
)

##################################
#    Read path (Grafana Cloud)   #
##################################

grafana_secrets = read_yaml_secrets(
    Path(f"grafana_cloud/api.{stack_info.env_suffix}.yaml")
)
if "cloud_access_policy_token" in grafana_secrets:
    read_path = pdc.build(
        stack_info=stack_info,
        grafana_secrets=grafana_secrets,
        grafana_cloud_stack_slug=archive_config.require("grafana_cloud_stack_slug"),
        namespace=NAMESPACE,
        k8s_labels=k8s_labels,
        node_selector=ARCHIVE_NODE_SELECTOR,
        tolerations=ARCHIVE_TOLERATIONS,
    )
    datasources.loki_datasource(
        stack_info=stack_info,
        namespace=NAMESPACE,
        retention_days=retention_days,
        read_path=read_path,
    )
    datasources.mimir_datasource(
        stack_info=stack_info,
        namespace=NAMESPACE,
        retention_days=retention_days,
        read_path=read_path,
    )
    export("pdc_network_id", read_path.network.pdc_network_id)
else:
    log.warn(
        "grafana_cloud/api.*.yaml has no cloud_access_policy_token: the PDC "
        "network and agent are not created until an org admin adds one."
    )

export("namespace", NAMESPACE)
export("archive_domain", archive_domain)
export("retention_days", retention_days)
export(
    "backends",
    {
        backend: {
            "bucket": backend_storage.bucket_name,
            "irsa_role_arn": backend_storage.irsa_role.role.arn,
            "service_account": backend_storage.service_account,
        }
        for backend, backend_storage in archive_storage.items()
    },
)
export("nlb_security_group_id", archive_ingress.security_group.id)
export("loki_push_url", f"https://{archive_domain}{LOKI_PUSH_PATH}")
export("loki_otlp_logs_url", f"https://{archive_domain}{OTLP_LOGS_PATH}")
export("mimir_push_url", f"https://{archive_domain}{PROMETHEUS_PUSH_PATH}")
export("mimir_otlp_metrics_url", f"https://{archive_domain}{OTLP_METRICS_PATH}")
