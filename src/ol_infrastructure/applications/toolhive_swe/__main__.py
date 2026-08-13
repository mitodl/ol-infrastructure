"""Deploy the software-engineering agent-class MCP workloads on the operations cluster.

This stack owns the ``toolhive-swe`` namespace: the ``MCPServer``, ``MCPGroup``, and
``VirtualMCPServer`` resources consumed by SWE/platform agents. The ToolHive operator
and CRDs that reconcile these resources are installed cluster-scoped by the
``ol-application-toolhive-operator`` stack; this stack references that one so it fails
fast if the operator has never been deployed.

See ``../toolhive_operator/DEPLOYMENT_STRATEGY.md`` for why agent classes are
separated by namespace under a single operator.

This is the initial CI-only deployment. It wires up ToolHive resources following
https://docs.stacklok.com/toolhive/guides-vmcp/quickstart and
https://docs.stacklok.com/toolhive/guides-vmcp/authentication:

- an ``MCPGroup`` (``swe-tools``) that groups the SWE backend MCP servers,
- the reference ``fetch`` ``MCPServer``
  (https://docs.stacklok.com/toolhive/guides-mcp/fetch), joined to that group via
  ``spec.groupRef``,
- the ``grafana`` ``MCPServer`` (OSS mcp-grafana pointed at Grafana Cloud with a
  service-account token from stack config; see mcp_servers.py for why the hosted
  Grafana Cloud MCP endpoint is not proxied instead), also joined to the group,
- the per-stack optional ``context7``, ``sentry`` and ``aws`` ``MCPServer``s, each
  gated behind a ``toolhive_swe:<name>_enabled`` boolean (see mcp_servers.py),
- an ``MCPOIDCConfig`` (``swe-vmcp-oidc``) used to validate the JWTs the vMCP's own
  embedded auth server issues (its issuer is the vMCP endpoint itself), and
- a ``VirtualMCPServer`` (``swe-vmcp``) that aggregates every backend in the group
  behind a single endpoint and fronts them with an embedded OAuth authorization
  server.

Incoming auth (browser login via Keycloak, brokered by ToolHive):
    Authentication uses ToolHive's EMBEDDED authorization server
    (``spec.authServerConfig``). The vMCP is the OAuth provider that MCP clients talk
    to: it exposes ``/authorize``, ``/token``, ``/register`` and
    ``/.well-known/oauth-authorization-server`` at its own URL and BROKERS interactive
    login to Keycloak (the ``ol-platform-engineering`` realm) as an upstream OIDC
    provider. ``spec.incomingAuth`` then validates the JWTs the embedded server issues
    (issuer == the vMCP endpoint) and advertises the protected-resource metadata
    (RFC 9728) that points clients at the embedded auth server.

    The end-to-end flow for a client such as Claude Code: hit the endpoint → get a
    401 pointing at the vMCP's own auth server → the client registers itself via
    Dynamic Client Registration (RFC 7591), so NO pre-registered client_id is needed
    on the client side (just the URL) → a browser opens to the vMCP's ``/authorize``,
    which redirects to Keycloak for login → Keycloak redirects back to the vMCP's
    ``/oauth/callback`` → the vMCP mints its own JWT → the client retries with that
    bearer token, which ``incomingAuth`` validates.

    Keycloak sees ONE ordinary CONFIDENTIAL web-app client (``ol-toolhive-client``,
    provisioned by the keycloak substructure) whose secret is synced from Vault
    (``secret-operations/sso/toolhive``) into this namespace by the Vault Secrets
    Operator and referenced as the upstream provider's ``clientSecretRef``. No
    Keycloak Dynamic Client Registration is required — DCR happens against the vMCP,
    not Keycloak.

    APISIX does NOT participate in authentication — it only terminates TLS and proxies
    every path (``/mcp``, ``/authorize``, ``/token``, ``/oauth/callback``,
    ``/.well-known/*``) through to the vMCP Service.

    Two pieces of state must persist for clients to stay authenticated across vMCP
    pod restarts, and both are provisioned here:
      * Signing material — ``authServerConfig.signingKeySecretRefs`` (an RSA-2048
        PKCS#8 PEM key) and ``hmacSecretRefs`` (a 256-bit base64 HMAC), read from
        encrypted stack config (``toolhive_swe:auth_server_signing_key`` /
        ``:auth_server_hmac_secret``) so they are stable across deploys. Without these
        the auth server uses ephemeral keys and previously issued tokens break on
        restart.
      * Session + DCR registration store — ``authServerConfig.storage.redis`` points
        at a small single-replica in-cluster Redis (StatefulSet + PVC, defined in this
        stack). Without it these live in memory and are wiped on restart, so DCR
        clients get ``invalid_client`` and must re-register. Redis runs with
        requirepass; the CRD requires a password (aclUserConfig.passwordSecretRef).

AWS access (for the ``aws`` backend):
    The ``aws`` backend is AWS's official ``mcp-proxy-for-aws`` SigV4 proxy in front
    of the managed AWS MCP Server endpoint. It authenticates with IRSA rather than a
    token in stack config: the ``OLEKSAuthBinding`` below creates the
    ``toolhive-swe-aws-mcp`` ServiceAccount annotated with the IRSA role ARN, the
    MCPServer's ``spec.serviceAccount`` puts the workload pod on it, and the EKS
    pod-identity webhook supplies the web-identity credentials boto3 picks up.

    Read-only is enforced entirely by that role's IAM — AWS-managed
    ``ReadOnlyAccess`` plus an explicit Deny on secret-material reads. The proxy's
    ``--read-only`` flag is deliberately not used; see mcp_servers.py for why it
    removes account access altogether rather than restricting it. IAM matters here
    because CI, QA and Production share one AWS account, so this role reads
    Production resources regardless of which stack created it.

The ``VirtualMCPServer`` is exposed to the internet through the shared APISIX gateway
on the operations cluster at ``toolhive-swe[.<env>].ol.mit.edu`` using the hybrid
HTTPRoute + ApisixTls pattern (ADR-0003). The hostname is added to the operations EKS
stack's ``eks:apisix_domains`` so external-dns points it at the APISIX NLB.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, export
from pulumi_aws import iam

from ol_infrastructure.applications.toolhive_swe.ingress import (
    create_ingress_resources,
)
from ol_infrastructure.applications.toolhive_swe.mcp_servers import (
    AWS_MCP_SERVICE_ACCOUNT_NAME,
    MCP_GROUP_NAME,
    create_mcp_servers,
)
from ol_infrastructure.applications.toolhive_swe.redis import (
    REDIS_PASSWORD_SECRET_KEY,
    REDIS_PASSWORD_SECRET_NAME,
    create_redis_resources,
    redis_addr,
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
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()

stack_info = parse_stack()

# Stack config. The signing key, HMAC secret, and Redis password are stored as
# ENCRYPTED stack config (``pulumi config set --secret``) rather than generated in
# Pulumi state, matching the repo's practice for managed secrets. See the generation
# commands where each is consumed below, and the placeholders in Pulumi.<env>.yaml.
toolhive_swe_config = Config("toolhive_swe")

# K8s stack reference + provider for the operations cluster.
cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Reference the operator stack and eagerly require its output so this stack fails
# fast if the ToolHive operator and its CRDs have not been deployed yet — the
# MCPServer CustomResource below cannot be reconciled without them.
operator_stack = make_stack_reference(projects.TOOLHIVE_OPERATOR, stack_info.name)
require_stack_output_value(operator_stack, "toolhive_namespace")

TOOLHIVE_NAMESPACE = "toolhive-swe"

# The namespace is provisioned by the EKS operations stack; fail fast if missing.
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(TOOLHIVE_NAMESPACE, ns)
)

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": f"operations-{stack_info.env_suffix}",
        "Application": "toolhive",
        "Owner": "platform-engineering",
    }
)

# Typed labels for OL component resources (e.g. the Vault auth binding).
k8s_labels = K8sGlobalLabels(
    service=Services.toolhive,
    ou=BusinessUnit.operations,
    stack=stack_info,
)
# Plain label dict applied to the raw K8s objects we manage directly.
k8s_global_labels = k8s_labels.model_dump()

# Public hostname the vMCP is served on. This is also the embedded auth server's
# issuer and the OAuth resource identifier ToolHive advertises + validates.
# Follows the per-environment convention toolhive-swe[.<env>].ol.mit.edu.
if stack_info.env_suffix == "production":
    VMCP_DOMAIN = "toolhive-swe.ol.mit.edu"
else:
    VMCP_DOMAIN = f"toolhive-swe.{stack_info.env_suffix}.ol.mit.edu"
VMCP_RESOURCE_URL = f"https://{VMCP_DOMAIN}"
# RFC 8707 resource identifier / token audience. MCP clients (e.g. Claude Code)
# canonicalize a bare origin with a trailing slash per WHATWG URL rules and send
# THAT as the `resource` parameter, so the registered audience must include the
# trailing slash or the embedded auth server rejects the token request with
# "resource is not a registered audience". (The AS issuer, by contrast, must NOT
# have a trailing slash, so it keeps using VMCP_RESOURCE_URL.)
VMCP_RESOURCE_ID = f"{VMCP_RESOURCE_URL}/"
# Where Keycloak redirects after the user logs in (handled by the vMCP broker).
VMCP_OAUTH_CALLBACK = f"{VMCP_RESOURCE_URL}/oauth/callback"

# Keycloak realm the embedded auth server brokers login to (upstream OIDC provider).
# The SSO hostname follows the per-environment convention sso[-<env>].ol.mit.edu.
if stack_info.env_suffix == "production":
    KEYCLOAK_DOMAIN = "sso.ol.mit.edu"
else:
    KEYCLOAK_DOMAIN = f"sso-{stack_info.env_suffix}.ol.mit.edu"
KEYCLOAK_ISSUER = f"https://{KEYCLOAK_DOMAIN}/realms/ol-platform-engineering"
OIDC_CLIENT_ID = "ol-toolhive-client"
MCP_OIDC_CONFIG_NAME = "swe-vmcp-oidc"

# K8s Secret (synced from Vault by VSO) holding the Keycloak client secret that the
# embedded auth server uses to broker to the upstream provider.
UPSTREAM_SECRET_NAME = "toolhive-swe-oidc-upstream"  # noqa: S105  # pragma: allowlist secret
UPSTREAM_SECRET_KEY = "client-secret"  # noqa: S105  # pragma: allowlist secret

# Persistent signing material for the embedded auth server. Generated once per
# environment and stored as encrypted stack config (via `pulumi config set --secret`)
# so it remains stable across deploys and pod restarts (vs. ephemeral keys generated
# at startup, which would invalidate previously issued tokens).
SIGNING_KEY_SECRET_NAME = "toolhive-swe-authserver-signing-key"  # noqa: S105  # pragma: allowlist secret
SIGNING_KEY_SECRET_KEY = "signing-key"  # noqa: S105  # pragma: allowlist secret
HMAC_SECRET_NAME = "toolhive-swe-authserver-hmac"  # noqa: S105  # pragma: allowlist secret
HMAC_SECRET_KEY = "hmac-key"  # noqa: S105  # pragma: allowlist secret

# In-cluster Redis backing the embedded auth server's persistent storage (OAuth
# sessions + DCR client registrations), so those survive vMCP pod restarts.
# Provisioned in redis.py; the vMCP spec below references the address and the
# password Secret.
REDIS_ADDR = redis_addr(TOOLHIVE_NAMESPACE)

#############################################
#   Vault auth binding + AWS read-only IRSA #
#############################################
# The binding provisions the Vault Secrets Operator wiring (VaultConnection /
# VaultAuth / sync service account) plus a Vault policy granting read access to the
# Keycloak client secret at secret-operations/sso/toolhive. It ALSO provisions the
# IRSA role and ServiceAccount the `aws` MCP backend runs under (see
# mcp_servers.py), which is the only thing in this namespace that touches AWS.
#
# AWS-managed ReadOnlyAccess is attached below rather than being spelled out as a
# policy document: reproducing it by hand would be thousands of actions to keep in
# sync with AWS. What is spelled out here is the Deny that carves back the
# data-plane reads ReadOnlyAccess includes — the ones that turn "read-only" into
# "can exfiltrate every credential we keep in AWS". An explicit Deny beats
# ReadOnlyAccess's Allow in IAM policy evaluation, so these stay unreachable even
# as AWS grows what the managed policy covers.
#
# The action list was derived empirically, not from the docs: on 2026-08-07 the
# live CI role was probed against each candidate with a deliberately nonexistent
# resource, so a 403 meant IAM blocks the action and any other error meant IAM
# permits it. Nine actions came back permitted; every one of them is below. Where
# a comment says "measured", that is what it refers to.
#
# Several denies are blunter than we would like because IAM offers no condition
# key to scope them — ec2:DescribeInstanceAttribute is one action for every
# attribute, apigateway:GET one action for every read, ssm:GetParameter* cannot
# distinguish SecureString from String. In each case the whole action goes, and
# the lost describe-level detail is an accepted cost.
#
# NOTE this is a denylist against a managed policy that AWS keeps growing, which
# makes it unbounded by construction: it closes what was measured, not what AWS
# ships next. Swapping the base grant to ViewOnlyAccess (List/Describe only, no
# resource contents) would make this class of gap structurally impossible and
# reduce this document to a short backstop. That is the durable fix if this ever
# needs revisiting.
aws_mcp_deny_policy_document = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "DenyCredentialMaterialReads",
            "Effect": "Deny",
            "Action": [
                "secretsmanager:GetSecretValue",
                "secretsmanager:BatchGetSecretValue",
                # GetParameterHistory returns the same values GetParameter does,
                # plus superseded ones. Measured: it was the one outright bypass
                # of this statement's original form, permitted by ReadOnlyAccess.
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:GetParametersByPath",
                "ssm:GetParameterHistory",
                "kms:Decrypt",
                # Service configuration that conventionally carries credentials
                # in plaintext environment variables. Every action in this group
                # was measured as permitted by ReadOnlyAccess. The list is long
                # because the surface is: any service that runs a container or a
                # build hands its environment back through a Describe/Get call.
                "lambda:GetFunction",
                "lambda:GetFunctionConfiguration",
                "ecs:DescribeTaskDefinition",
                "codebuild:BatchGetProjects",
                "apprunner:DescribeService",
                "batch:DescribeJobDefinitions",
                "amplify:GetApp",
                "amplify:GetBranch",
                "elasticbeanstalk:DescribeConfigurationSettings",
                "sagemaker:DescribeTrainingJob",
                "sagemaker:DescribeProcessingJob",
                # Glue job DefaultArguments routinely hold connection strings.
                "glue:GetJob",
                # EC2 user-data, where this repo's Consul/Vault bootstrap config
                # lives. DescribeInstanceAttribute covers a RUNNING instance;
                # launch templates and launch configurations are a second door to
                # the same script and are what an ASG actually renders from, so
                # denying only the first would leave the data reachable.
                "ec2:DescribeInstanceAttribute",
                "ec2:DescribeLaunchTemplateVersions",
                "autoscaling:DescribeLaunchConfigurations",
                # Other surfaces that embed values inline.
                "cloudformation:GetTemplate",
                "ssm:GetDocument",
                # EMR's IAM prefix is elasticmapreduce, not the boto3 name emr.
                "elasticmapreduce:ListBootstrapActions",
                # Credential-minting reads. Measured as blocked today, but only
                # because ReadOnlyAccess does not happen to grant them; pinned so
                # a future expansion of that managed policy cannot open them.
                # ecr:GetAuthorizationToken is the exception to "measured": it
                # takes no arguments, so probing it would have minted a real
                # 12-hour registry credential. It is denied on inference from the
                # ecr:Get* family that ReadOnlyAccess grants.
                "ecr:GetAuthorizationToken",
                "redshift:GetClusterCredentials",
                "redshift-serverless:GetCredentials",
                "glue:GetConnection",
                # sso:GetRoleCredentials deliberately NOT listed: the SSO portal
                # API is authorized by an SSO access token rather than by IAM
                # identity-based policy, so denying it here would be a no-op.
                # Parliament rejects it as an unknown action for that reason.
                # One action covers /apikeys?includeValues=true, which returns
                # API key material.
                "apigateway:GET",
            ],
            "Resource": "*",
        },
        {
            "Sid": "DenyBulkDataPlaneReads",
            "Effect": "Deny",
            "Action": [
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:GetObjectTorrent",
                # Athena and Redshift read the data lake through their OWN
                # service roles and return rows over their APIs, so the s3
                # denies above do not cover them — a second door to the same
                # room the ol-data-lake-* buckets are in.
                "athena:GetQueryResults",
                "redshift-data:GetStatementResult",
                "rds-data:ExecuteStatement",
                "rds-data:BatchExecuteStatement",
                "rds-data:ExecuteSql",
                "dynamodb:GetItem",
                "dynamodb:BatchGetItem",
                "dynamodb:Query",
                "dynamodb:Scan",
                "dynamodb:PartiQLSelect",
                "dynamodb:GetRecords",
                # sqs:ReceiveMessage is not even read-only in effect: it starts a
                # visibility timeout and can stall a real consumer.
                "kinesis:GetRecords",
                "kinesis:GetShardIterator",
                "kinesis:SubscribeToShard",
                "sqs:ReceiveMessage",
                # Log bodies are the most common accidental secret store. Denied
                # at a real cost to debugging, but CloudWatch log content stays
                # reachable through the grafana backend, which reads it under its
                # own service account rather than this role.
                "logs:GetLogEvents",
                "logs:FilterLogEvents",
                "logs:GetLogRecord",
                "logs:GetQueryResults",
            ],
            "Resource": "*",
        },
    ],
}

# The ServiceAccount and the ReadOnlyAccess attachment are gated on the same
# boolean as the backend itself, because a ServiceAccount is NOT bound to one
# workload: Kubernetes lets any pod in the namespace select it by name and
# receive its projected token. Leaving it in place in a stack where the backend
# is disabled would mean anyone able to create an MCPServer CR in toolhive-swe
# could set spec.serviceAccount to it and obtain shared-account AWS reads —
# turning namespace-level workload creation into Production-wide access with the
# feature switched off. Creating the SA only where the backend runs closes that:
# Kubernetes refuses to schedule a pod naming a ServiceAccount that is absent.
#
# The trust role and the Deny policy are still created everywhere. Both are
# genuinely inert on their own — a role with nothing but a Deny attached grants
# nothing — and keeping them stack-invariant means promotion stays a one-line
# config change.
aws_mcp_enabled = toolhive_swe_config.get_bool("aws_mcp_enabled")

toolhive_swe_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="toolhive-swe",
        namespace=TOOLHIVE_NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=aws_mcp_deny_policy_document,
        # A Deny on "*" is the point of this document, not an oversight.
        parliament_config={"RESOURCE_EFFECTIVELY_STAR": {}},
        vault_policy_path=Path(__file__).parent.joinpath("toolhive_swe_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name=AWS_MCP_SERVICE_ACCOUNT_NAME,
        create_irsa_service_account=aws_mcp_enabled,
        vault_sync_service_account_names="toolhive-swe-vault",
        k8s_labels=k8s_labels,
    )
)

# The read grant itself. Broad on purpose: the value of an AWS backend is being
# able to describe whatever an agent is debugging without a human relaying CLI
# output. The Deny above is what keeps "describe everything" from also meaning
# "read every secret".
#
# Gated with the ServiceAccount so a stack with the backend disabled has no path
# to these permissions at all, rather than a dormant one.
if aws_mcp_enabled:
    iam.RolePolicyAttachment(
        f"toolhive-swe-aws-mcp-readonly-attach-{stack_info.env_suffix}",
        policy_arn="arn:aws:iam::aws:policy/ReadOnlyAccess",
        role=toolhive_swe_auth_binding.irsa_role.name,
    )

# Sync the Keycloak client secret from Vault into a namespace-local K8s Secret so
# ToolHive's embedded auth server can reference it as the upstream clientSecretRef.
upstream_oidc_secret = OLVaultK8SSecret(
    f"toolhive-swe-oidc-upstream-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=UPSTREAM_SECRET_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=UPSTREAM_SECRET_NAME,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/toolhive",
        exclude_raw=True,
        excludes=[".*"],
        templates={UPSTREAM_SECRET_KEY: '{{ get .Secrets "client_secret" }}'},
        refresh_after="1h",
        vaultauth=toolhive_swe_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=toolhive_swe_auth_binding.vault_k8s_resources,
    ),
)

##############################################
#   Embedded auth server persistent keys      #
##############################################
# Signing material from encrypted stack config so it is stable across deploys (and
# not regenerated), which keeps issued tokens valid across vMCP pod restarts.
# Generate + set (per environment):
#   openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
#     | pulumi config set --secret toolhive_swe:auth_server_signing_key --
#   openssl rand -base64 32 \
#     | pulumi config set --secret toolhive_swe:auth_server_hmac_secret --
# RSA-2048 PKCS#8 PEM signing key used by the embedded auth server to sign JWTs.
authserver_signing_key_pem = toolhive_swe_config.require_secret(
    "auth_server_signing_key"
)
# 256-bit base64 HMAC secret.
authserver_hmac_secret_value = toolhive_swe_config.require_secret(
    "auth_server_hmac_secret"
)

# Materialise both as K8s Secrets referenced by authServerConfig below.
authserver_signing_key_secret = kubernetes.core.v1.Secret(
    f"toolhive-swe-authserver-signing-key-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=SIGNING_KEY_SECRET_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    type="Opaque",
    string_data={SIGNING_KEY_SECRET_KEY: authserver_signing_key_pem},
    opts=ResourceOptions(delete_before_replace=True),
)
authserver_hmac_key_secret = kubernetes.core.v1.Secret(
    f"toolhive-swe-authserver-hmac-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=HMAC_SECRET_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    type="Opaque",
    string_data={HMAC_SECRET_KEY: authserver_hmac_secret_value},
    opts=ResourceOptions(delete_before_replace=True),
)

##############################################
#   In-cluster Redis (embedded AS storage)    #
##############################################
# Password Secret + headless Service + StatefulSet, defined in redis.py.
redis_resources = create_redis_resources(
    stack_info=stack_info,
    namespace=TOOLHIVE_NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    toolhive_swe_config=toolhive_swe_config,
)

#########################################
#   MCPGroup + backend MCPServers        #
#########################################
# The ``swe-tools`` MCPGroup and every backend MCPServer that joins it, defined
# in mcp_servers.py. The VirtualMCPServer below aggregates the group's backends.
mcp_servers = create_mcp_servers(
    stack_info=stack_info,
    namespace=TOOLHIVE_NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    cluster_stack=cluster_stack,
    toolhive_swe_config=toolhive_swe_config,
    # A list, not a single handle: it is empty in stacks where the backend is
    # disabled, which is exactly the stacks that build no MCPServer to depend on.
    aws_mcp_service_accounts=toolhive_swe_auth_binding.irsa_service_accounts,
)

#########################################
#   MCPOIDCConfig (incoming validation)  #
#########################################
# Validates the JWTs issued by the vMCP's own embedded auth server, so the issuer
# is the vMCP endpoint itself (NOT Keycloak — Keycloak is the upstream the embedded
# server brokers to). Referenced by the VirtualMCPServer's incomingAuth below.
mcp_oidc_config = kubernetes.apiextensions.CustomResource(
    f"toolhive-swe-mcp-oidc-config-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="MCPOIDCConfig",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=MCP_OIDC_CONFIG_NAME,
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "type": "inline",
        "inline": {
            "issuer": VMCP_RESOURCE_URL,
        },
    },
    opts=ResourceOptions(depends_on=[cluster_stack]),
)

#########################################
#   VirtualMCPServer aggregator          #
#########################################
# Aggregates every backend in the ``swe-tools`` group behind a single endpoint and
# fronts them with an embedded OAuth authorization server that brokers login to
# Keycloak. Tool-name collisions across backends are resolved by prefixing with the
# workload name.
swe_virtualmcpserver = kubernetes.apiextensions.CustomResource(
    f"toolhive-swe-vmcp-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="VirtualMCPServer",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="swe-vmcp",
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "groupRef": {"name": MCP_GROUP_NAME},
        # Embedded auth server: the vMCP is the OAuth provider MCP clients talk to,
        # brokering interactive login to Keycloak as an upstream OIDC provider.
        "authServerConfig": {
            "issuer": VMCP_RESOURCE_URL,
            # Scopes every DCR-registered client is allowed to request at
            # /oauth/authorize regardless of what it registered with. MCP clients
            # (e.g. Claude Code) auto-request ``offline_access`` for refresh tokens
            # once the AS advertises it, so it must be permitted here or the AS
            # rejects the authorization with ``invalid_scope``. Kept deliberately
            # narrow (openid + offline_access); every value must also be in the
            # upstream-derived scope set below or the auth server fails to start.
            "baselineClientScopes": ["openid", "offline_access"],
            # Persistent signing material so issued tokens survive pod restarts
            # (omitting these makes the auth server generate ephemeral keys).
            "signingKeySecretRefs": [
                {"name": SIGNING_KEY_SECRET_NAME, "key": SIGNING_KEY_SECRET_KEY}
            ],
            "hmacSecretRefs": [{"name": HMAC_SECRET_NAME, "key": HMAC_SECRET_KEY}],
            # Persistent storage for OAuth sessions + DCR client registrations, so
            # clients don't have to re-register/re-auth after a vMCP pod restart.
            "storage": {
                "type": "redis",
                "redis": {
                    "addr": REDIS_ADDR,
                    # Password-only AUTH (usernameSecretRef omitted) against the
                    # requirepass-protected in-cluster Redis.
                    "aclUserConfig": {
                        "passwordSecretRef": {
                            "name": REDIS_PASSWORD_SECRET_NAME,
                            "key": REDIS_PASSWORD_SECRET_KEY,
                        },
                    },
                },
            },
            "upstreamProviders": [
                {
                    "name": "keycloak",
                    "type": "oidc",
                    "oidcConfig": {
                        "issuerUrl": KEYCLOAK_ISSUER,
                        "clientId": OIDC_CLIENT_ID,
                        "clientSecretRef": {
                            "name": UPSTREAM_SECRET_NAME,
                            "key": UPSTREAM_SECRET_KEY,
                        },
                        "redirectUri": VMCP_OAUTH_CALLBACK,
                        # offline_access so ToolHive obtains a refresh token from
                        # Keycloak and so it appears in the upstream-derived scope
                        # set that baselineClientScopes is validated against.
                        "scopes": ["openid", "profile", "email", "offline_access"],
                    },
                }
            ],
        },
        # Validate the JWTs the embedded auth server issues.
        "incomingAuth": {
            "type": "oidc",
            "oidcConfigRef": {
                "name": MCP_OIDC_CONFIG_NAME,
                # Trailing-slash form: matches the RFC 8707 resource MCP clients
                # actually send (see VMCP_RESOURCE_ID).
                "audience": VMCP_RESOURCE_ID,
                "resourceUrl": VMCP_RESOURCE_ID,
            },
        },
        "serviceType": "ClusterIP",
        "config": {
            "aggregation": {
                "conflictResolution": "prefix",
                "conflictResolutionConfig": {"prefixFormat": "{workload}_"},
            },
        },
    },
    opts=ResourceOptions(
        depends_on=[
            mcp_servers.group,
            *mcp_servers.servers,
            mcp_oidc_config,
            upstream_oidc_secret,
            authserver_signing_key_secret,
            authserver_hmac_key_secret,
            redis_resources.password_secret,
            redis_resources.service,
            redis_resources.statefulset,
        ]
    ),
)

#########################################
#   Internet exposure via APISIX         #
#########################################
# TLS certificate + ApisixTls + HTTPRoute, defined in ingress.py. APISIX only
# terminates TLS and proxies through to the vMCP Service — all auth (OAuth
# endpoints + token validation) happens inside the vMCP.
vmcp_cert, vmcp_httproute = create_ingress_resources(
    stack_info=stack_info,
    namespace=TOOLHIVE_NAMESPACE,
    k8s_global_labels=k8s_global_labels,
    vmcp_domain=VMCP_DOMAIN,
    swe_virtualmcpserver=swe_virtualmcpserver,
)

export("toolhive_namespace", TOOLHIVE_NAMESPACE)
export("mcp_group_name", MCP_GROUP_NAME)
export("vmcp_domain", VMCP_DOMAIN)
export("vmcp_oauth_issuer", VMCP_RESOURCE_URL)
export("vmcp_upstream_issuer", KEYCLOAK_ISSUER)
