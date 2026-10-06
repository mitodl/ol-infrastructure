"""Deploy Apache Gravitino as the data lake's Iceberg REST catalog.

Gravitino fronts the existing Glue tables through a CUSTOM ``GlueCatalog``
backend, authenticates every request with the caller's own Keycloak token, and
vends per-table, per-user S3 credentials. The design, and the reasoning behind
each choice here, is docs/plans/gravitino-deployment-spec.md (decisions P1-P10).
Identity settings come from docs/plans/gravitino-keycloak-integration-spec.md
(D4-D7).

Pulumi does not create the metalake, the catalog or any grant. Those belong to
the grant reconciler CronJob this stack deploys
(docs/plans/gravitino-authorization-spec.md, A7), which is the only workload
holding the management API's client certificate.
"""

import hashlib
import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, Output, ResourceOptions, export
from pulumi_aws import ec2, get_caller_identity, iam

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import (
    GRAVITINO_CHART_VERSION,
    GRAVITINO_SCHEMA_VERSION,
    GRAVITINO_VERSION,
)
from ol_infrastructure.applications.gravitino.posture import create_posture_probe
from ol_infrastructure.applications.gravitino.reconcile import (
    create_grant_reconciler,
    render_desired_state,
)
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultPostgresDatabaseConfig,
    OLVaultRestartTarget,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import (
    IAM_POLICY_VERSION,
    cross_environment_glue_denial,
    data_lake_bucket_arns,
    data_lake_glue_resources,
    lint_iam_policy,
)
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()
gravitino_config = Config("gravitino")

cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")
setup_k8s_provider(require_stack_output_value(cluster_stack, "kube_config"))
network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
data_vpc = network_stack.require_output("data_vpc")
vault_infra_stack = make_stack_reference(
    projects.VAULT_SERVER, f"operations.{stack_info.name}"
)
data_warehouse_stack = make_stack_reference(projects.DATA_WAREHOUSE, stack_info.name)

NAMESPACE = "gravitino"
RELEASE_NAME = "gravitino"
SERVICE_ACCOUNT_NAME = "gravitino"
SERVICE_HOST = f"{RELEASE_NAME}.{NAMESPACE}.svc"
ICEBERG_REST_PORT = 9433
MANAGEMENT_PORT = 8433
MANAGEMENT_URI = f"https://{SERVICE_HOST}:{MANAGEMENT_PORT}"
AWS_REGION = "us-east-1"
METALAKE = "ol_data_platform"
CATALOG = f"ol_data_lake_{stack_info.env_suffix}"
SERVICE_ADMIN = "service-account-ol-gravitino-admin"
DB_NAME = "gravitino"
CONFIG_MAP_KEY = "gravitino.conf"

TLS_MOUNT_PATH = "/etc/gravitino/tls"
# Jetty reads its keystore once at startup and nothing restarts the Deployment
# when cert-manager renews the Secret, so a 90-day leaf would expire under a
# long-lived pod. Every certificate here lives as long as the CA instead.
CERTIFICATE_DURATION = "87600h"
CA_SECRET_NAME = "gravitino-ca"  # pragma: allowlist secret  # noqa: S105
SERVER_TLS_SECRET_NAME = (
    "gravitino-server-tls"  # pragma: allowlist secret  # noqa: S105
)
CLIENT_TLS_SECRET_NAME = (
    "gravitino-reconcile-client-tls"  # pragma: allowlist secret  # noqa: S105
)
KEYSTORE_PASSWORD_SECRET_NAME = (
    "gravitino-keystore-password"  # pragma: allowlist secret  # noqa: S105
)
DB_CREDS_SECRET_NAME = "gravitino-db-creds"  # pragma: allowlist secret  # noqa: S105

# gravitino.conf is a plain properties file: Gravitino reads no environment
# variables into it, and file values override -D system properties, so neither a
# Secret reference nor JAVA_OPTS can supply a credential. The chart therefore
# renders these placeholders, and the init script below replaces them from env
# vars sourced from Secrets before exec'ing the image entrypoint (spec P3).
JDBC_USER_PLACEHOLDER = "__GRAVITINO_JDBC_USER__"
JDBC_PASSWORD_PLACEHOLDER = (
    "__GRAVITINO_JDBC_PASSWORD__"  # pragma: allowlist secret  # noqa: S105
)
KEYSTORE_PASSWORD_PLACEHOLDER = (
    "__GRAVITINO_KEYSTORE_PASSWORD__"  # pragma: allowlist secret  # noqa: S105
)

aws_config = AWSBase(
    tags={"OU": "data", "Environment": f"data-{stack_info.env_suffix}"}
)
k8s_app_labels = K8sAppLabels(
    application=Application.gravitino,
    product=Product.data,
    service=Services.gravitino,
    ou=BusinessUnit.data,
    source_repository="https://github.com/apache/gravitino",
    stack=stack_info,
)
k8s_labels = k8s_app_labels.model_dump()

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(NAMESPACE, ns)
)

replicas = gravitino_config.require_int("replicas")
keycloak_issuer = gravitino_config.require("keycloak_issuer")

################################
# IAM: pod role and vending role
################################
# The layer buckets only. The landing zone holds raw Airbyte files, not catalog
# tables, and has no reason to be reachable by a vended credential.
lake_bucket_arns = data_lake_bucket_arns(stack_info.env_suffix)
data_lake_object_actions = [
    "s3:AbortMultipartUpload",
    "s3:DeleteObject",
    "s3:GetBucketLocation",
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:ListBucket",
    "s3:ListBucketMultipartUploads",
    "s3:ListMultipartUploadParts",
    "s3:PutObject",
]

# Gravitino's own catalog IO (metadata writes, GlueCatalog calls) runs on the
# pod's IRSA role. A dedicated policy rather than data_lake_query_engine_iam_policy,
# which also grants Bedrock, ListAllMyBuckets and Glue tagging on "*". Scoped to
# this environment's lake only: one Gravitino serves one environment (spec P2).
gravitino_pod_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "glue:CreateDatabase",
                "glue:CreateTable",
                "glue:DeleteDatabase",
                "glue:DeleteTable",
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetTable",
                "glue:GetTables",
                "glue:UpdateDatabase",
                "glue:UpdateTable",
            ],
            # DeleteDatabase also drops the database's functions, so IAM
            # requires the userDefinedFunction ARNs for it.
            "Resource": data_lake_glue_resources(
                stack_info.env_suffix,
                resource_types=("database", "table", "userDefinedFunction"),
            ),
        },
        {
            "Effect": "Allow",
            "Action": data_lake_object_actions,
            "Resource": lake_bucket_arns,
        },
    ],
}

gravitino_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="gravitino",
        namespace=NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=gravitino_pod_policy_document,
        vault_policy_path=Path(__file__).parent.joinpath("gravitino_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name=SERVICE_ACCOUNT_NAME,
        vault_sync_service_account_names=["gravitino-vault"],
        k8s_labels=k8s_app_labels,
        # The chart takes a ServiceAccount name and never creates one.
        create_irsa_service_account=True,
    )
)

if cross_environment_glue_denial(stack_info.env_suffix):
    iam.RolePolicyAttachment(
        f"gravitino-{stack_info.env_suffix}-cross-environment-glue-denial",
        policy_arn=data_warehouse_stack.require_output(
            "data_lake_cross_environment_glue_denial_policy_arn"
        ),
        role=gravitino_auth_binding.irsa_role.name,
        opts=ResourceOptions(parent=gravitino_auth_binding),
    )

# The role aws-irsa vends from (catalog property s3-role-arn). For a non-table
# request aws-irsa returns this role's credentials with no session policy, so it
# holds lake object storage and nothing else: no Glue, no Bedrock, no bucket
# listing (spec P6). AssumeRoleWithWebIdentity authenticates with the pod's
# projected token rather than chaining from the pod role, so it trusts the same
# ServiceAccount. Its default 3600s max session must stay >= the catalog's
# s3-token-expire-in-secs.
gravitino_vending_role = OLEKSTrustRole(
    f"gravitino-vending-irsa-trust-role-{stack_info.env_suffix}",
    role_config=OLEKSTrustRoleConfig(
        account_id=get_caller_identity().account_id,
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        description="Gravitino credential vending role, scoped to data lake objects",
        policy_operator="StringEquals",
        role_name="gravitino-vending",
        service_account_name=SERVICE_ACCOUNT_NAME,
        service_account_namespace=NAMESPACE,
        tags=aws_config.tags,
    ),
)
gravitino_vending_policy = iam.Policy(
    f"gravitino-vending-policy-{stack_info.env_suffix}",
    name=f"gravitino-vending-policy-{stack_info.env_suffix}",
    path=f"/ol-data/gravitino-vending-policy-{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": data_lake_object_actions,
                    "Resource": lake_bucket_arns,
                }
            ],
        },
        stringify=True,
    ),
    description="Data lake object access vended by Gravitino through aws-irsa",
    opts=ResourceOptions(parent=gravitino_vending_role),
)
iam.RolePolicyAttachment(
    f"gravitino-vending-policy-attachment-{stack_info.env_suffix}",
    policy_arn=gravitino_vending_policy.arn,
    role=gravitino_vending_role.role.name,
    opts=ResourceOptions(parent=gravitino_vending_role),
)

################################
# Entity store: RDS Postgres 16
################################
gravitino_db_security_group = ec2.SecurityGroup(
    f"gravitino-rds-security-group-{stack_info.env_suffix}",
    name_prefix=f"gravitino-rds-{stack_info.env_suffix}-",
    description="Grant access to the Gravitino entity store",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"],
            description="Grant access to RDS from EKS pods",
        ),
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            security_groups=[
                vault_infra_stack.require_output("vault_server")["security_group"]
            ],
            description="Grant access to RDS from Vault",
        ),
    ],
    tags=aws_config.merged_tags({"Name": f"gravitino-rds-{stack_info.env_suffix}"}),
    vpc_id=data_vpc["id"],
)

rds_defaults = defaults(stack_info)["rds"]
# Metadata pointers and grants, not an application database: the Production
# default (m7g.large plus a read replica) is sized for the latter.
rds_defaults["instance_size"] = gravitino_config.require("db_instance_size")
rds_defaults["read_replica"] = None
# Production enables Enhanced Monitoring, whose role would otherwise land at
# "/", outside the IAM paths the Concourse infra worker may create roles under.
rds_defaults["enhanced_monitoring_role_path"] = "/ol-infrastructure/rds/"
gravitino_db_config = OLPostgresDBConfig(
    instance_name=f"ol-gravitino-db-{stack_info.env_suffix}",
    password=gravitino_config.require("db_password"),
    subnet_group_name=data_vpc["rds_subnet"],
    security_groups=[gravitino_db_security_group],
    tags=aws_config.tags,
    db_name=DB_NAME,
    # Gravitino tests Postgres 12 through 16 and calls anything else "not tested
    # fully" (docs/how-to-use-relational-backend-storage.md). The entity store is
    # the catalog's source of truth, so stay on a tested major until it moves.
    engine_major_version="16",
    **rds_defaults,
)
gravitino_db = OLAmazonDB(gravitino_db_config)

gravitino_db_vault_backend = OLVaultDatabaseBackend(
    OLVaultPostgresDatabaseConfig(
        db_name=DB_NAME,
        mount_point=f"{gravitino_db_config.engine}-gravitino",
        db_admin_username=gravitino_db_config.username,
        db_admin_password=gravitino_config.require("db_password"),
        db_host=gravitino_db.db_instance.address,
    )
)

# creds/app mints a new username as well as a new password on every lease, so
# both are substituted. Rotation restarts the Deployment, which re-runs the
# substitution.
gravitino_db_creds = OLVaultK8SSecret(
    f"gravitino-db-creds-{stack_info.env_suffix}",
    OLVaultK8SDynamicSecretConfig(
        name=DB_CREDS_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_labels,
        dest_secret_labels=k8s_labels,
        dest_secret_name=DB_CREDS_SECRET_NAME,
        mount=gravitino_db_vault_backend.db_mount.path,
        path="creds/app",
        exclude_raw=True,
        templates={
            "DB_USER": "{{ .Secrets.username }}",
            "DB_PASS": "{{ .Secrets.password }}",
        },
        restart_targets=[OLVaultRestartTarget(kind="Deployment", name=RELEASE_NAME)],
        vaultauth=gravitino_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[gravitino_auth_binding.vault_k8s_resources],
    ),
)

################################
# TLS: namespace-local CA
################################
# The CA exists only to be trusted by Gravitino (client certificates on the
# management port) and by StarRocks (the Iceberg REST port), so it stays out of
# the cluster-wide issuers (spec P7).
selfsigned_issuer = kubernetes.apiextensions.CustomResource(
    f"gravitino-selfsigned-issuer-{stack_info.env_suffix}",
    api_version="cert-manager.io/v1",
    kind="Issuer",
    metadata={
        "name": "gravitino-selfsigned",
        "namespace": NAMESPACE,
        "labels": k8s_labels,
    },
    spec={"selfSigned": {}},
)
ca_certificate = kubernetes.apiextensions.CustomResource(
    f"gravitino-ca-certificate-{stack_info.env_suffix}",
    api_version="cert-manager.io/v1",
    kind="Certificate",
    metadata={"name": "gravitino-ca", "namespace": NAMESPACE, "labels": k8s_labels},
    spec={
        "isCA": True,
        "commonName": f"gravitino-ca-{stack_info.env_suffix}",
        "secretName": CA_SECRET_NAME,
        # Leaves embed this CA in their truststores at issuance, so a CA renewal
        # breaks trust until every leaf is reissued. Ten years keeps that a
        # planned event rather than a surprise.
        "duration": CERTIFICATE_DURATION,
        "privateKey": {"algorithm": "ECDSA", "size": 256},
        "issuerRef": {
            "group": "cert-manager.io",
            "kind": "Issuer",
            "name": "gravitino-selfsigned",
        },
    },
    opts=ResourceOptions(depends_on=[selfsigned_issuer]),
)
ca_issuer = kubernetes.apiextensions.CustomResource(
    f"gravitino-ca-issuer-{stack_info.env_suffix}",
    api_version="cert-manager.io/v1",
    kind="Issuer",
    metadata={"name": "gravitino-ca", "namespace": NAMESPACE, "labels": k8s_labels},
    spec={"ca": {"secretName": CA_SECRET_NAME}},
    opts=ResourceOptions(depends_on=[ca_certificate]),
)

keystore_password_secret = kubernetes.core.v1.Secret(
    f"gravitino-keystore-password-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=KEYSTORE_PASSWORD_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_labels,
    ),
    # A stack secret like StarRocks' keystore password; the repo has no
    # pulumi_random. It must not contain a backslash: Gravitino reads it back out
    # of gravitino.conf with Properties.load, which treats one as an escape, while
    # cert-manager uses it raw.
    string_data={"password": gravitino_config.require_secret("keystore_password")},
)

# keystore.p12 holds the server key and chain; truststore.p12 holds the CA and is
# what the management port checks client certificates against.
server_certificate = OLCertManagerCert(
    f"gravitino-server-cert-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="gravitino",
        resource_suffix="server",
        k8s_namespace=NAMESPACE,
        k8s_labels=k8s_labels,
        issuer_name="gravitino-ca",
        dest_secret_name=SERVER_TLS_SECRET_NAME,
        duration=CERTIFICATE_DURATION,
        dns_names=[SERVICE_HOST, f"{SERVICE_HOST}.cluster.local"],
        pkcs12_keystore_password_secret_name=KEYSTORE_PASSWORD_SECRET_NAME,
    ),
    opts=ResourceOptions(depends_on=[ca_issuer, keystore_password_secret]),
)

# Mounted only by the grant reconciler. Whoever can read Secrets in this
# namespace can take it, so namespace RBAC is the real boundary around the
# management API.
client_certificate = OLCertManagerCert(
    f"gravitino-reconcile-client-cert-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="gravitino",
        resource_suffix="reconcile-client",
        k8s_namespace=NAMESPACE,
        k8s_labels=k8s_labels,
        issuer_name="gravitino-ca",
        dest_secret_name=CLIENT_TLS_SECRET_NAME,
        duration=CERTIFICATE_DURATION,
        dns_names=[f"gravitino-reconcile.{NAMESPACE}.svc"],
        usages=["digital signature", "key encipherment", "client auth"],
    ),
    opts=ResourceOptions(depends_on=[ca_issuer]),
)

################################
# Gravitino server
################################
oauth_settings = {
    "serviceAudience": "ol-gravitino-catalog",
    # authority is the issuer check. Blank means no check (Keycloak spec D6).
    "authority": keycloak_issuer,
    "jwksUri": f"{keycloak_issuer}/protocol/openid-connect/certs",
    "tokenValidatorClass": (
        "org.apache.gravitino.server.authentication.JwksTokenValidator"
    ),
    "principalFields": "starrocks_username,preferred_username",
    "groupsFields": "role_keys",
    "groupMapper": "regex",
    "groupMapperRegexPattern": "^(.*)$",
    # The chart defaults these to a sample realm path and a test audience. Blank
    # drops the key from the rendered file.
    "tokenPath": "",
    "serverUri": "",
    "defaultSignKey": "",
}

# Keys the chart has no dedicated value for. Anything with a dedicated value
# (authenticators, authorization, auxService, icebergRest, audit) goes through
# it instead: the chart always renders its own default for those, so a second
# copy here would leave last-one-wins deciding whether the catalog is secured.
https_settings = {
    f"{prefix}.{key}": value
    for prefix in ("gravitino.server.webserver", "gravitino.iceberg-rest")
    for key, value in {
        "enableHttps": "true",
        "keyStorePath": f"{TLS_MOUNT_PATH}/keystore.p12",
        "keyStoreType": "PKCS12",
        "keyStorePassword": KEYSTORE_PASSWORD_PLACEHOLDER,
        "managerPassword": KEYSTORE_PASSWORD_PLACEHOLDER,
    }.items()
}
additional_config_items = {
    **https_settings,
    "gravitino.server.webserver.httpsPort": str(MANAGEMENT_PORT),
    "gravitino.iceberg-rest.httpsPort": str(ICEBERG_REST_PORT),
    # Required client certificates on the management API only. Any USE_CATALOG
    # holder could otherwise fetch the vending role's unscoped credentials from
    # /objects/catalog/{c}/credentials (spec P6).
    "gravitino.server.webserver.enableClientAuth": "true",
    "gravitino.server.webserver.trustStorePath": f"{TLS_MOUNT_PATH}/truststore.p12",
    "gravitino.server.webserver.trustStoreType": "PKCS12",
    "gravitino.server.webserver.trustStorePassword": KEYSTORE_PASSWORD_PLACEHOLDER,
    "gravitino.authenticator.oauth.allowSkewSecs": "30",
    # Stops the job system fetching template files from internal addresses. The
    # default is already true; stating it lets the posture probe assert it.
    "gravitino.fetchFile.blockUnsafeRemoteUri": "true",
}
if replicas > 1:
    # The entity cache has no change-log hook, so a peer replica can resolve a
    # dropped-and-recreated name to the old id until the TTL lapses (spec P9).
    # This duplicates a key the chart renders, which is unavoidable: its template
    # is `{{ .Values.cache.enabled | default true }}`, and Helm's `default`
    # treats false as empty, so cache.enabled=false renders `true`. The last
    # occurrence wins when Gravitino loads the file.
    additional_config_items["gravitino.cache.enabled"] = "false"

# Each occurs exactly once in the rendered file with this value, or the posture
# probe fails.
expected_security_settings = {
    "gravitino.authenticators": "oauth",
    "gravitino.authenticator.oauth.authority": keycloak_issuer,
    "gravitino.authenticator.oauth.serviceAudience": oauth_settings["serviceAudience"],
    "gravitino.authorization.enable": "true",
    "gravitino.authorization.serviceAdmins": SERVICE_ADMIN,
    "gravitino.server.webserver.enableHttps": "true",
    "gravitino.server.webserver.enableClientAuth": "true",
    "gravitino.iceberg-rest.enableHttps": "true",
    "gravitino.fetchFile.blockUnsafeRemoteUri": "true",
}
# PassThroughAuthorizer turns every check off even with authorization enabled.
forbidden_settings = ["gravitino.authorization.impl"]
# Both default to allowedOrigins=* with allowCredentials=true when enabled.
must_not_be_true_settings = [
    "gravitino.server.webserver.enableCorsFilter",
    "gravitino.iceberg-rest.enableCorsFilter",
]

init_script = """set -euo pipefail
cp /tmp/conf/* "${GRAVITINO_HOME}/conf"
conf="${GRAVITINO_HOME}/conf/gravitino.conf"
for var in GRAVITINO_JDBC_USER GRAVITINO_JDBC_PASSWORD GRAVITINO_KEYSTORE_PASSWORD; do
  value="${!var:?${var} is not set}"
  rendered="$(<"${conf}")"
  printf '%s\\n' "${rendered//__${var}__/"${value}"}" > "${conf}"
done
# The entrypoint links iceberg-bundles/ (the Glue SDK) into both lib dirs.
# Without it the GlueCatalog backend cannot load.
exec /bin/bash "${GRAVITINO_HOME}/docker/docker-entrypoint.sh"
"""


def _secret_env(name: str, secret: str, key: str) -> dict[str, object]:
    return {
        "name": name,
        "valueFrom": {"secretKeyRef": {"name": secret, "key": key}},
    }


def _https_probe(path: str, initial_delay_seconds: int) -> dict[str, object]:
    # The kubelet does not verify the certificate and cannot present a client
    # certificate, so the probes use the Iceberg REST port, not 8433.
    return {
        "httpGet": {"path": path, "port": ICEBERG_REST_PORT, "scheme": "HTTPS"},
        "initialDelaySeconds": initial_delay_seconds,
        "periodSeconds": 10,
        "timeoutSeconds": 5,
        "failureThreshold": 6,
    }


gravitino_registry, _, gravitino_repository = cached_image_uri(
    "apache/gravitino"
).partition("/")

gravitino_values = {
    "fullnameOverride": RELEASE_NAME,
    "image": {
        "registry": gravitino_registry,
        "repository": gravitino_repository,
        "tag": GRAVITINO_VERSION,
    },
    "replicas": replicas,
    "serviceAccountName": SERVICE_ACCOUNT_NAME,
    "podLabels": k8s_labels,
    "entity": {
        "jdbcUrl": gravitino_db.db_instance.address.apply(
            lambda address: (
                f"jdbc:postgresql://{address}:{DEFAULT_POSTGRES_PORT}/{DB_NAME}"
                "?currentSchema=public&sslmode=require"
            )
        ),
        "jdbcDriver": "org.postgresql.Driver",
        "jdbcUser": JDBC_USER_PLACEHOLDER,
        "jdbcPassword": JDBC_PASSWORD_PLACEHOLDER,
    },
    "authorization": {"enable": True, "serviceAdmins": SERVICE_ADMIN},
    "authenticators": "oauth",
    "authenticator": {"oauth": oauth_settings},
    "auxService": {"names": "iceberg-rest"},
    "icebergRest": {
        # In dynamic mode the S3 and backend settings belong on the catalog, which
        # the reconciler sets. Leave icebergRest.s3 unset: the chart renders
        # s3-access-key-id even when it is blank.
        "catalogConfigProvider": "dynamic-config-provider",
        "dynamicConfigProvider": {
            "metalake": METALAKE,
            "defaultCatalogName": CATALOG,
        },
    },
    "audit": {
        "enabled": True,
        "formatter": {"className": "org.apache.gravitino.audit.JsonAuditFormatter"},
    },
    "additionalConfigItems": additional_config_items,
    # Audit lines go to the gravitino.audit logger. Give it its own console
    # appender, bare JSON and not additive, so it reaches Loki as a separate,
    # parseable stream instead of interleaved with server logs.
    "additionalLog4j2Properties": {
        "appender.console.type": "Console",
        "appender.console.name": "consoleLogger",
        "appender.console.layout.type": "PatternLayout",
        "appender.console.layout.pattern": (
            "%d{yyyy-MM-dd HH:mm:ss} %-5p [%t] %c{1}:%L - %m%n"
        ),
        "rootLogger.appenderRef.console.ref": "consoleLogger",
        "appender.audit.type": "Console",
        "appender.audit.name": "auditLogger",
        "appender.audit.layout.type": "PatternLayout",
        "appender.audit.layout.pattern": "%m%n",
        "logger.audit.name": "gravitino.audit",
        "logger.audit.level": "info",
        "logger.audit.additivity": "false",
        "logger.audit.appenderRef.audit.ref": "auditLogger",
    },
    "initScript": init_script,
    "env": [
        # docker-entrypoint.sh adds -XX:-UseContainerSupport, so the JVM ignores
        # the cgroup limit. Replacing the chart default also drops its
        # MaxMetaspaceSize, so the configured value must carry its own.
        {"name": "GRAVITINO_MEM", "value": gravitino_config.require("jvm_memory")},
        {"name": "AWS_REGION", "value": AWS_REGION},
        _secret_env("GRAVITINO_JDBC_USER", DB_CREDS_SECRET_NAME, "DB_USER"),
        _secret_env("GRAVITINO_JDBC_PASSWORD", DB_CREDS_SECRET_NAME, "DB_PASS"),
        _secret_env(
            "GRAVITINO_KEYSTORE_PASSWORD", KEYSTORE_PASSWORD_SECRET_NAME, "password"
        ),
    ],
    "resources": {
        "requests": {
            "cpu": gravitino_config.require("cpu_request"),
            "memory": gravitino_config.require("memory_limit"),
        },
        "limits": {"memory": gravitino_config.require("memory_limit")},
    },
    "extraVolumes": [
        {"name": "gravitino-log", "emptyDir": {}},
        {
            "name": "gravitino-tls",
            "secret": {
                "secretName": SERVER_TLS_SECRET_NAME,
                "items": [
                    {"key": "keystore.p12", "path": "keystore.p12"},
                    {"key": "truststore.p12", "path": "truststore.p12"},
                ],
                # The image runs as uid 1000 in group 0. Both stores are
                # password-protected.
                "defaultMode": 0o444,
            },
        },
    ],
    "extraVolumeMounts": [
        {"name": "gravitino-log", "mountPath": "/opt/gravitino/logs"},
        {"name": "gravitino-tls", "mountPath": TLS_MOUNT_PATH, "readOnly": True},
    ],
    "service": {
        "name": RELEASE_NAME,
        "type": "ClusterIP",
        "port": ICEBERG_REST_PORT,
        "targetPort": ICEBERG_REST_PORT,
        "portName": "iceberg-https",
    },
    "extraExposePorts": [
        {
            "port": MANAGEMENT_PORT,
            "protocol": "TCP",
            # The chart also uses this as the container port name, which
            # Kubernetes caps at 15 characters.
            "name": "mgmt-https",
            "targetPort": MANAGEMENT_PORT,
        }
    ],
    "readinessProbe": _https_probe("/iceberg/health/ready", 20),
    "livenessProbe": _https_probe("/iceberg/health/live", 60),
    "affinity": {
        "podAntiAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "weight": 100,
                    "podAffinityTerm": {
                        "labelSelector": {
                            "matchLabels": {
                                "app": "gravitino-helm",
                                "release": RELEASE_NAME,
                            }
                        },
                        "topologyKey": "kubernetes.io/hostname",
                    },
                }
            ]
        }
    },
    # minAvailable: 1 at two replicas allows one disruption at a time. Do not set
    # maxUnavailable alongside it: the chart renders both and the API server
    # rejects a PDB with both.
    "podDisruptionBudget": {"enabled": replicas > 1},
}

################################
# Schema
################################
# Gravitino only creates its schema on H2. The upstream schema file is not safe to
# rerun (eight bare CREATE INDEX statements), so the Job skips when the schema is
# already there. Version upgrades are manual SQL with the server stopped
# (docs/how-to-upgrade.md), shipped in the same PR as the chart bump.
schema_file = f"schema-{GRAVITINO_SCHEMA_VERSION}-postgresql.sql"
schema_exists_query = "SELECT to_regclass('public.metalake_meta')"
# SET ROLE so the tables are owned by the app role, which the Vault revoke path
# reassigns to. One transaction (-1), because the skip guard keys on the first
# table the file creates: a partial apply must leave nothing behind for a retry
# to mistake for a finished one.
SCHEMA_SCRIPT = f"""
if [ -n "$(psql -tAc "{schema_exists_query}")" ]; then
  echo "Gravitino schema already present; skipping."
  exit 0
fi
psql -1 -v ON_ERROR_STOP=1 -c "SET ROLE {DB_NAME}" -f "/schema/{schema_file}"
"""
schema_job = kubernetes.batch.v1.Job(
    f"gravitino-schema-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        namespace=NAMESPACE,
        labels=k8s_labels,
    ),
    spec=kubernetes.batch.v1.JobSpecArgs(
        backoff_limit=3,
        active_deadline_seconds=600,
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=k8s_labels),
            spec=kubernetes.core.v1.PodSpecArgs(
                restart_policy="Never",
                automount_service_account_token=False,
                init_containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="copy-schema",
                        image=f"{gravitino_registry}/{gravitino_repository}:{GRAVITINO_VERSION}",
                        command=[
                            "cp",
                            f"/opt/gravitino/scripts/postgresql/{schema_file}",
                            "/schema/",
                        ],
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="schema", mount_path="/schema"
                            )
                        ],
                    )
                ],
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="apply-schema",
                        image=f"{cached_image_uri('postgres')}:16-alpine",
                        command=[
                            "/bin/sh",
                            "-ec",
                            SCHEMA_SCRIPT,
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGHOST", value=gravitino_db.db_instance.address
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGDATABASE", value=DB_NAME
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGSSLMODE", value="require"
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGUSER",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name=DB_CREDS_SECRET_NAME, key="DB_USER"
                                    )
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="PGPASSWORD",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name=DB_CREDS_SECRET_NAME, key="DB_PASS"
                                    )
                                ),
                            ),
                        ],
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="schema", mount_path="/schema", read_only=True
                            )
                        ],
                    )
                ],
                volumes=[
                    kubernetes.core.v1.VolumeArgs(
                        name="schema",
                        empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
                    )
                ],
            ),
        ),
    ),
    opts=ResourceOptions(depends_on=[gravitino_db_creds]),
)

gravitino_release = kubernetes.helm.v3.Release(
    f"gravitino-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name=RELEASE_NAME,
        chart="oci://registry-1.docker.io/apache/gravitino-helm",
        version=GRAVITINO_CHART_VERSION,
        namespace=NAMESPACE,
        cleanup_on_fail=True,
        skip_await=False,
        values=gravitino_values,
    ),
    opts=ResourceOptions(
        depends_on=[
            schema_job,
            server_certificate,
            *gravitino_auth_binding.irsa_service_accounts,
        ],
    ),
)

# The collector is not given the namespace CA: that would mean letting it read
# Secrets in the namespace that holds the reconciler's client certificate. The
# scrape carries no credential, so skipping verification exposes nothing but
# the metrics themselves.
kubernetes.apiextensions.CustomResource(
    f"gravitino-service-monitor-{stack_info.env_suffix}",
    api_version="monitoring.coreos.com/v1",
    kind="ServiceMonitor",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=RELEASE_NAME,
        namespace=NAMESPACE,
        labels={**k8s_labels, "release": "prometheus"},
    ),
    spec={
        "selector": {"matchLabels": {"app": "gravitino-helm", "release": RELEASE_NAME}},
        "namespaceSelector": {"matchNames": [NAMESPACE]},
        "endpoints": [
            {
                "port": "iceberg-https",
                "path": "/prometheus/metrics",
                "scheme": "https",
                "tlsConfig": {"insecureSkipVerify": True},
                "interval": "60s",
                "scrapeTimeout": "10s",
            }
        ],
    },
    opts=ResourceOptions(depends_on=[gravitino_release]),
)

create_posture_probe(
    stack_info=stack_info,
    namespace=NAMESPACE,
    k8s_labels=k8s_labels,
    gravitino_host=SERVICE_HOST,
    iceberg_rest_port=ICEBERG_REST_PORT,
    management_port=MANAGEMENT_PORT,
    tls_secret_name=SERVER_TLS_SECRET_NAME,
    config_map_name=RELEASE_NAME,
    config_map_key=CONFIG_MAP_KEY,
    expected_settings=Output.from_input(expected_security_settings),
    forbidden_settings=forbidden_settings,
    must_not_be_true_settings=must_not_be_true_settings,
    gravitino_release=gravitino_release,
    release_fingerprint=Output.from_input(gravitino_values).apply(
        lambda values: hashlib.sha256(
            json.dumps(values, sort_keys=True).encode()
        ).hexdigest()
    ),
)

# Off until an environment's token and grant verification has passed
# (authorization spec, "Verification before this is called done"). A failing run
# alerts through WorkloadJobFailed*.
if gravitino_config.require_bool("reconcile_enabled"):
    create_grant_reconciler(
        stack_info=stack_info,
        namespace=NAMESPACE,
        k8s_labels=k8s_labels,
        management_uri=MANAGEMENT_URI,
        client_tls_secret_name=CLIENT_TLS_SECRET_NAME,
        vault_auth_name=gravitino_auth_binding.vault_k8s_resources.auth_name,
        desired_state=gravitino_vending_role.role.arn.apply(
            lambda vending_role_arn: render_desired_state(
                env_suffix=stack_info.env_suffix,
                metalake=METALAKE,
                catalog=CATALOG,
                # Deployment spec P5 and P6. In dynamic-config-provider mode the
                # backend and vending settings live on the catalog, not in
                # gravitino.conf.
                catalog_properties={
                    "catalog-backend": "custom",
                    "catalog-backend-impl": "org.apache.iceberg.aws.glue.GlueCatalog",
                    # Required by the property validator, ignored by GlueCatalog.
                    "uri": f"https://glue.{AWS_REGION}.amazonaws.com",
                    # Gravitino refuses to load a schema without it. GlueCatalog
                    # only uses it to place tables in a database that has no
                    # location of its own, so it points where dbt puts the
                    # databases it creates.
                    "warehouse": (
                        f"s3://ol-data-lake-staging-{stack_info.env_suffix}/processed"
                    ),
                    "credential-providers": "aws-irsa",
                    "s3-role-arn": vending_role_arn,
                    "s3-region": AWS_REGION,
                    "s3-token-expire-in-secs": "3600",
                    # GlueCatalog cannot serve the metadata cache. Blank stops
                    # the warning Gravitino logs about it.
                    "table-metadata-cache-impl": "",
                },
            )
        ),
        depends_on=[
            gravitino_release,
            client_certificate,
            gravitino_auth_binding.vault_k8s_resources,
        ],
    )

export(
    "gravitino",
    {
        "iceberg_rest_uri": f"https://{SERVICE_HOST}:{ICEBERG_REST_PORT}/iceberg",
        "management_uri": MANAGEMENT_URI,
        "metalake": METALAKE,
        "catalog": CATALOG,
        "namespace": NAMESPACE,
        "ca_secret_name": CA_SECRET_NAME,
        "reconcile_client_tls_secret_name": CLIENT_TLS_SECRET_NAME,
        "pod_role_arn": gravitino_auth_binding.irsa_role.arn,
        "vending_role_arn": gravitino_vending_role.role.arn,
        "db_address": gravitino_db.db_instance.address,
    },
)
