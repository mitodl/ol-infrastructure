"""Deploy omnigraph-server: the S3-backed graph service that backs witan.

``omnigraph-server`` (an external Rust binary,
https://github.com/ModernRelay/omnigraph — not vendored in either repo) is a
stateless service whose entire state lives in S3. This stack owns the
``omnigraph`` namespace and deploys a single ``Deployment`` + ClusterIP
``Service`` (``data_tier.py``), an ``OLBucket`` reached via IRSA, and the
generated ``cluster.yaml`` ConfigMap that keeps the bucket name/region and
graph list in lockstep with the Pulumi-managed bucket.

This is deliberately a standalone service, not part of the witan MCP stack:
witan (``applications/witan``) is one consumer of it, reaching this
Deployment over the cluster network via a ``StackReference`` to this stack's
``omnigraph_server_addr`` output. ToolHive is not involved here at all — it
only runs the witan MCP tier, which is an implementation detail of that stack,
not this one.

Access to the graph is gated by omnigraph-server's own bearer-token auth
(``OMNIGRAPH_SERVER_BEARER_TOKENS_FILE``), whose ``{actor_id: token}`` map is
the same artifact witan resolves per-user tokens from — synced here from Vault
into the ``actor-tokens`` Secret (agent-kit ADR-0004 D3). See
``docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md``.

Follow-up work this stack does NOT cover (tracked separately):
    - **Container image.** omnigraph-server has no image built in either repo
      yet; this stack only provisions the ECR repository and references
      ``:latest`` (the ``omnigraph``/``pulumi-omnigraph`` Concourse pipeline
      builds and pushes it). ``schema.pg`` (agent-kit repo,
      ``mcp/servers/witan/schema/schema.pg``) must be baked into the image at
      build time — this Pulumi program has no access to agent-kit's tree.
    - **Keycloak witan-users token sync.** This stack provisions the
      ``actor-tokens`` Secret destination but not the job that keeps per-user
      entries current as Keycloak group membership changes.
"""

from pathlib import Path

from pulumi import ResourceOptions, export

from ol_infrastructure.applications.omnigraph.data_tier import (
    create_data_tier,
    omnigraph_server_addr,
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
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import make_stack_reference, parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()

stack_info = parse_stack()

cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

NAMESPACE = "omnigraph"

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(NAMESPACE, ns)
)

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": f"operations-{stack_info.env_suffix}",
        "Application": "omnigraph",
        "Owner": "platform-engineering",
    }
)

k8s_labels = K8sGlobalLabels(
    service=Services.omnigraph,
    ou=BusinessUnit.operations,
    stack=stack_info,
)
k8s_global_labels = k8s_labels.model_dump()

# {actor_id: token} JSON map omnigraph-server boots its bearer-token auth from
# (OMNIGRAPH_SERVER_BEARER_TOKENS_FILE). The same artifact witan resolves
# per-user tokens from in its own namespace — both sync from the one Vault
# source (secret-operations/witan/actor-tokens), agent-kit ADR-0004 D3.
ACTOR_TOKENS_SECRET_NAME = "actor-tokens"  # noqa: S105  # pragma: allowlist secret
ACTOR_TOKENS_SECRET_KEY = "tokens.json"  # noqa: S105  # pragma: allowlist secret

##############################################
#   Vault auth binding (IRSA + VSO sync)      #
##############################################
# omnigraph-server needs AWS access for its S3-backed store (IRSA below;
# data_tier.py attaches the bucket policy once the OLBucket ARN is known) plus
# the Vault Secrets Operator sync wiring for the actor-tokens Secret.
omnigraph_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="omnigraph",
        namespace=NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath("omnigraph_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="omnigraph-server",
        create_irsa_service_account=True,
        vault_sync_service_account_names=["omnigraph-server-vault"],
        k8s_labels=k8s_labels,
    )
)

actor_tokens_secret = OLVaultK8SSecret(
    f"omnigraph-actor-tokens-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=ACTOR_TOKENS_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=ACTOR_TOKENS_SECRET_NAME,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path="witan/actor-tokens",
        exclude_raw=True,
        excludes=[".*"],
        templates={ACTOR_TOKENS_SECRET_KEY: '{{ get .Secrets "tokens_json" }}'},
        refresh_after="15m",
        vaultauth=omnigraph_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=omnigraph_auth_binding.vault_k8s_resources,
    ),
)

#########################################
#   omnigraph-server data tier           #
#########################################
data_tier = create_data_tier(
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    aws_config=aws_config,
    auth_binding=omnigraph_auth_binding,
    actor_tokens_secret_name=ACTOR_TOKENS_SECRET_NAME,
    actor_tokens_secret=actor_tokens_secret,
)

export("namespace", NAMESPACE)
export("omnigraph_server_addr", omnigraph_server_addr(NAMESPACE))
export(
    "omnigraph_server_ecr_repository_url",
    data_tier.ecr_repository.repository_url,
)
