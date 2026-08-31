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
- an ``MCPOIDCConfig`` (``swe-vmcp-oidc``) used to validate the JWTs Keycloak issues
  directly to MCP clients, and
- a ``VirtualMCPServer`` (``swe-vmcp``) that aggregates every backend in the group
  behind a single endpoint and validates those Keycloak-issued bearer tokens.

Incoming auth (MCP clients register and log in with Keycloak directly):
    ``spec.incomingAuth`` (type ``oidc``) is the ONLY auth surface on the vMCP; there
    is no embedded authorization server. The vMCP is a pure OAuth resource server: it
    advertises RFC 9728 protected-resource metadata pointing at Keycloak (the
    ``ol-platform-engineering`` realm) as the authorization server, and validates
    bearer tokens Keycloak issued against ``MCP_OIDC_CONFIG_NAME``'s issuer/JWKS.

    The end-to-end flow for a client such as Claude Code: hit the endpoint → get a
    401 whose ``WWW-Authenticate``/protected-resource metadata names Keycloak as the
    authorization server → the client discovers Keycloak's own
    ``/.well-known/openid-configuration`` and registers itself directly against
    Keycloak's native Dynamic Client Registration endpoint (RFC 7591,
    ``/realms/ol-platform-engineering/clients-registrations/openid-connect``) → a
    browser opens to Keycloak's own ``/protocol/openid-connect/auth`` for login →
    Keycloak issues the access token straight to the client (PKCE, no client secret
    for these public/native registrations) → the client calls the vMCP with that
    bearer token, which ``incomingAuth`` validates against Keycloak's issuer.

    This has no vMCP-side OAuth state to persist: no embedded auth server, no signing
    keys, no DCR client store, and therefore no Redis. Keycloak's own (already-HA)
    datastore is what now holds client registrations and sessions. This replaces the
    prior embedded-auth-server design, which stored its DCR registrations in a
    single-replica in-cluster Redis with no PodDisruptionBudget; when QA's node pool
    churned that Redis pod's zone-pinned volume, DCR lookups failed and clients saw
    ``invalid_client``. See ol-infrastructure tk-switch-toolhive-swe-vmcp-incomingauth
    -to-validat-b8e450.

    OPERATOR ACTION REQUIRED per environment after this apply: the removed
    StatefulSet's ``volumeClaimTemplates`` PVC (``data-toolhive-swe-redis-0``, a
    100Gi EBS volume) is NOT deleted by Kubernetes' default retention behavior —
    it was never a standalone Pulumi resource, so Pulumi cannot clean it up either.
    Once the switch to Keycloak-direct auth is confirmed working, manually run
    ``kubectl delete pvc data-toolhive-swe-redis-0 -n toolhive-swe`` (per cluster)
    or it keeps billing for an unused volume indefinitely.

    Audience: Keycloak 26.7.2 (the pinned KEYCLOAK_VERSION) does not implement RFC
    8707 resource indicators — confirmed via the upstream keycloak/keycloak issue
    tracker, where support is tracked as in-progress for milestone 26.8.0 (issue
    #51413 and related PRs), not yet shipped. So a token issued to a
    dynamically-registered client carries no ``aud`` claim matching this vMCP's
    resource URL by default. The keycloak substructure
    (``ol_platform_engineering.py``, TOOLHIVE block) provisions an OPTIONAL client
    scope (``toolhive-swe-audience``, realm-wide via ``RealmOptionalClientScopes``)
    with an ``AudienceProtocolMapper`` that stamps ``VMCP_RESOURCE_ID`` onto the
    access token when requested. ``incomingAuth.oidcConfigRef.scopes`` below
    advertises it in the RFC 9728 protected-resource metadata so a compliant DCR
    client requests it automatically; a client that ignores the advertisement and
    never requests the scope will fail ``incomingAuth``'s audience check.

    STILL UNVERIFIED (not something this repo can confirm — needs a live check in
    the Keycloak admin console before this is applied): whether the realm's
    Anonymous client-registration policy set's ``Trusted Hosts`` policy is
    configured permissively enough to allow unauthenticated DCR from MCP clients
    calling from arbitrary IPs (an empty trusted-hosts list with "host sending
    registration request must match" enabled would block DCR outright).

    APISIX does NOT participate in authentication — it only terminates TLS and proxies
    every path (``/mcp``, ``/.well-known/*``) through to the vMCP Service.

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
    TOOLHIVE_SERVICE,
    create_mcp_servers,
)
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
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
from ol_infrastructure.lib.toolhive_telemetry import (
    telemetry_config_name,
    toolhive_service_name,
    toolhive_telemetry_spec,
    toolhive_vmcp_audit,
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

# Public hostname the vMCP is served on — the OAuth resource identifier
# ``incomingAuth`` validates. Follows the per-environment convention
# toolhive-swe[.<env>].ol.mit.edu.
if stack_info.env_suffix == "production":
    VMCP_DOMAIN = "toolhive-swe.ol.mit.edu"
else:
    VMCP_DOMAIN = f"toolhive-swe.{stack_info.env_suffix}.ol.mit.edu"
VMCP_RESOURCE_URL = f"https://{VMCP_DOMAIN}"
# RFC 8707 resource identifier / token audience. MCP clients (e.g. Claude Code)
# canonicalize a bare origin with a trailing slash per WHATWG URL rules and send
# THAT as the `resource` parameter, so the audience checked here must include the
# trailing slash.
VMCP_RESOURCE_ID = f"{VMCP_RESOURCE_URL}/"

# Keycloak realm MCP clients register against and authenticate with directly (no
# vMCP-side broker). The SSO hostname follows the per-environment convention
# sso[-<env>].ol.mit.edu.
if stack_info.env_suffix == "production":
    KEYCLOAK_DOMAIN = "sso.ol.mit.edu"
else:
    KEYCLOAK_DOMAIN = f"sso-{stack_info.env_suffix}.ol.mit.edu"
KEYCLOAK_ISSUER = f"https://{KEYCLOAK_DOMAIN}/realms/ol-platform-engineering"
MCP_OIDC_CONFIG_NAME = "swe-vmcp-oidc"

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

#########################################
#   MCPTelemetryConfig (ToolHive tier)   #
#########################################
# Every hop here runs its own OTel pipeline, configured through these CRs. See
# lib/toolhive_telemetry.py for the shared decisions; this stack only chooses
# which hop gets which.
#
# ★ TWO CRs, NOT ONE SHARED, and the difference is `prometheus.enabled`.
# ToolHive serves /metrics on the MAIN TRANSPORT PORT, and the vMCP's 4483 is
# the port APISIX publishes under a `/*` catch-all (ingress.py) — so enabling
# the path there would put an unauthenticated metrics endpoint on the public
# internet. The backends' 8080 is ClusterIP-only, so it is safe there.
#
# All five backends share the backend CR: they want identical settings and are
# separated by the per-ref `serviceName` (mcp_servers._observability).
swe_telemetry_backend = kubernetes.apiextensions.CustomResource(
    f"toolhive-swe-telemetry-backend-{stack_info.env_suffix}",
    api_version="toolhive.stacklok.dev/v1beta1",
    kind="MCPTelemetryConfig",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=telemetry_config_name(TOOLHIVE_SERVICE, "backend"),
        namespace=TOOLHIVE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    # Never None for this hop: Prometheus is on in every environment, so
    # `toolhive_telemetry_spec` always returns a spec here.
    spec=toolhive_telemetry_spec(
        stack_info, TOOLHIVE_SERVICE, "mcp-proxy", expose_prometheus=True
    ),
    opts=ResourceOptions(depends_on=[cluster_stack]),
)

# The vMCP's, only where there is an OTLP receiver to export to. With Prometheus
# off and OTLP unavailable there is nothing left to configure, so CI gets no CR
# and no `telemetryConfigRef` — rather than an inert one that reads as though
# telemetry were on.
swe_telemetry_vmcp_spec = toolhive_telemetry_spec(
    stack_info, TOOLHIVE_SERVICE, "vmcp", expose_prometheus=False
)
swe_telemetry_vmcp = (
    kubernetes.apiextensions.CustomResource(
        f"toolhive-swe-telemetry-vmcp-{stack_info.env_suffix}",
        api_version="toolhive.stacklok.dev/v1beta1",
        kind="MCPTelemetryConfig",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=telemetry_config_name(TOOLHIVE_SERVICE, "vmcp"),
            namespace=TOOLHIVE_NAMESPACE,
            labels=k8s_global_labels,
        ),
        spec=swe_telemetry_vmcp_spec,
        opts=ResourceOptions(depends_on=[cluster_stack]),
    )
    if swe_telemetry_vmcp_spec
    else None
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
    telemetry_config=swe_telemetry_backend,
)

#########################################
#   MCPOIDCConfig (incoming validation)  #
#########################################
# Validates JWTs Keycloak issues directly to MCP clients, so the issuer is Keycloak
# itself (NOT the vMCP — there is no embedded auth server to self-issue tokens
# anymore). jwksUrl is left to OIDC discovery against this issuer. Referenced by the
# VirtualMCPServer's incomingAuth below.
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
            "issuer": KEYCLOAK_ISSUER,
        },
    },
    opts=ResourceOptions(depends_on=[cluster_stack]),
)

#########################################
#   VirtualMCPServer aggregator          #
#########################################
# Aggregates every backend in the ``swe-tools`` group behind a single endpoint and
# validates bearer tokens Keycloak issued directly to MCP clients. Tool-name
# collisions across backends are resolved by prefixing with the workload name.
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
        # No authServerConfig: the vMCP is a pure resource server. MCP clients do
        # RFC 7591 DCR and login directly against Keycloak; there is no vMCP-side
        # OAuth state (no signing keys, no DCR store, no Redis) to provision here.
        "incomingAuth": {
            "type": "oidc",
            "oidcConfigRef": {
                "name": MCP_OIDC_CONFIG_NAME,
                # Trailing-slash form: matches the RFC 8707 resource MCP clients
                # actually send (see VMCP_RESOURCE_ID).
                "audience": VMCP_RESOURCE_ID,
                "resourceUrl": VMCP_RESOURCE_ID,
                # Advertised in RFC 9728 protected-resource metadata so a compliant
                # DCR client requests these at Keycloak automatically.
                # offline_access for refresh tokens; toolhive-swe-audience is the
                # optional client scope (keycloak substructure, TOOLHIVE block)
                # that stamps VMCP_RESOURCE_ID onto the token's aud claim — without
                # it Keycloak issues no audience this incomingAuth check accepts.
                "scopes": ["openid", "offline_access", "toolhive-swe-audience"],
            },
        },
        "serviceType": "ClusterIP",
        # Spans and OTLP metrics for the OUTERMOST hop — the first thing a
        # client's request touches, and so the root of the trace. Unpacked from
        # a dict rather than written as a literal key because the ref must not
        # appear at all when there is no CR to point at: an unresolvable
        # `telemetryConfigRef` degrades the vMCP rather than being ignored.
        **(
            {
                "telemetryConfigRef": {
                    "name": telemetry_config_name(TOOLHIVE_SERVICE, "vmcp"),
                    "serviceName": toolhive_service_name(
                        stack_info, TOOLHIVE_SERVICE, "vmcp"
                    ),
                }
            }
            if swe_telemetry_vmcp
            else {}
        ),
        "config": {
            "aggregation": {
                "conflictResolution": "prefix",
                "conflictResolutionConfig": {"prefixFormat": "{workload}_"},
            },
            # Per-request JSON to stdout -> pod logs -> Loki, so unlike the OTLP
            # block above this needs no collector and is on in CI too. This CRD
            # carries the full option set, so the body exclusion is stated
            # rather than merely defaulted — see `toolhive_vmcp_audit`.
            "audit": toolhive_vmcp_audit(),
        },
    },
    opts=ResourceOptions(
        depends_on=[
            mcp_servers.group,
            *mcp_servers.servers,
            mcp_oidc_config,
            # Absent in CI, where the vMCP references no telemetry config.
            *([swe_telemetry_vmcp] if swe_telemetry_vmcp else []),
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
export("vmcp_resource_id", VMCP_RESOURCE_ID)
export("vmcp_oidc_issuer", KEYCLOAK_ISSUER)
