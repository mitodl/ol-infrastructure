"""Deploy Hindsight as the shared memory service on the applications cluster.

One memory per learner, shared by every app, plus general (non-learner) memory.
Hindsight is used for storage and retrieval only: callers produce their notes
through ol-llm, and the server runs with no LLM provider, chunks-mode retain,
and observations and consolidation off, so it makes no LLM calls of its own.
Embeddings come from a text-embeddings-inference server in the same namespace,
so no learner text leaves the cluster.

Every request carries a Keycloak token for the memory service's audience. The
OL extensions in ol_memory_ext/ enforce who may touch which bank:

- an app acts for a learner only with a token exchanged (RFC 8693) from that
  learner's session, and reaches that learner's bank alone;
- general memory takes a client-credentials token carrying the general scope;
- the platform admin client may only run the forget sequence.

The service is cluster-internal: there is no route, and callers reach it at
``hindsight.hindsight.svc.cluster.local``.
"""

import hashlib
from pathlib import Path
from string import Template

import pulumi_kubernetes as kubernetes
from pulumi import Config, Output, ResourceOptions, export
from pulumi_aws import ec2
from pydantic import SecretStr

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import HINDSIGHT_VERSION, TEXT_EMBEDDINGS_INFERENCE_VERSION
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultPostgresDatabaseConfig,
    OLVaultRestartTarget,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    Application,
    AWSBase,
    BusinessUnit,
    Component,
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
from ol_infrastructure.lib.vault import postgres_role_statements, setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()
hindsight_config = Config("hindsight")

cluster_stack = make_stack_reference(projects.EKS, f"applications.{stack_info.name}")
setup_k8s_provider(require_stack_output_value(cluster_stack, "kube_config"))
network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
apps_vpc = network_stack.require_output("applications_vpc")
vault_infra_stack = make_stack_reference(
    projects.VAULT_SERVER, f"operations.{stack_info.name}"
)

NAMESPACE = "hindsight"
API_NAME = "hindsight"
API_PORT = 8888
EMBEDDINGS_NAME = "hindsight-embeddings"
EMBEDDINGS_PORT = 8080
SERVICE_ACCOUNT_NAME = "hindsight"
DB_NAME = "hindsight"
DB_CREDS_SECRET_NAME = "hindsight-db-creds"  # pragma: allowlist secret  # noqa: S105
EXTENSION_PACKAGE = "ol_memory_ext"
EXTENSION_ROOT = "/opt/ol_ext"
EXTENSION_SOURCE = Path(__file__).parent / EXTENSION_PACKAGE / "__init__.py"
MODEL_CACHE_PATH = "/data"

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"applications-{stack_info.env_suffix}"}
)
k8s_app_labels = K8sAppLabels(
    application=Application.hindsight,
    product=Product.infrastructure,
    service=Services.hindsight,
    ou=BusinessUnit.operations,
    source_repository="https://github.com/vectorize-io/hindsight",
    stack=stack_info,
)
k8s_labels = k8s_app_labels.model_dump()

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(NAMESPACE, ns)
)

keycloak_issuer = hindsight_config.require("keycloak_issuer")
alert_tier = AlertTier(hindsight_config.require("alert_tier"))

################################
# Database: RDS Postgres with pgvector
################################
hindsight_db_security_group = ec2.SecurityGroup(
    f"hindsight-rds-security-group-{stack_info.env_suffix}",
    name_prefix=f"hindsight-rds-{stack_info.env_suffix}-",
    description="Grant access to the Hindsight database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            cidr_blocks=apps_vpc["k8s_pod_subnet_cidrs"],
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
    tags=aws_config.merged_tags({"Name": f"hindsight-rds-{stack_info.env_suffix}"}),
    vpc_id=apps_vpc["id"],
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = hindsight_config.require("db_instance_size")
rds_defaults["read_replica"] = None
# Production enables Enhanced Monitoring, whose role would otherwise land at
# "/", outside the IAM paths the Concourse infra worker may create roles under.
rds_defaults["enhanced_monitoring_role_path"] = "/ol-infrastructure/rds/"
hindsight_db_config = OLPostgresDBConfig(
    instance_name=f"ol-hindsight-db-{stack_info.env_suffix}",
    password=SecretStr(hindsight_config.require("db_password")),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[hindsight_db_security_group],
    tags=aws_config.tags,
    db_name=DB_NAME,
    **rds_defaults,
)
hindsight_db = OLAmazonDB(hindsight_db_config)

# Hindsight's migrations need two things the default app role lacks. pgvector
# has to exist before they run (they check for it and stop), and installing it
# takes rds_superuser, which these statements run as. The migrations also issue
# CREATE SCHEMA IF NOT EXISTS "public", which Postgres checks against CREATE on
# the database even when the schema exists. Built as a copy: the defaults are
# shared by every stack.
hindsight_app_create_statements = [
    *postgres_role_statements["app"]["create"][:1],
    Template(
        """
        DO
        $$do$$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION
            WHEN unique_violation THEN
                RAISE NOTICE 'Extension "vector" was just created concurrently.';
        END
        $$do$$;
        """
    ),
    Template("""GRANT CREATE ON DATABASE "${app_name}" TO "${app_name}";"""),
    *postgres_role_statements["app"]["create"][1:],
]
hindsight_db_vault_backend = OLVaultDatabaseBackend(
    OLVaultPostgresDatabaseConfig(
        db_name=DB_NAME,
        mount_point=f"{hindsight_db_config.engine}-hindsight",
        db_admin_username=hindsight_db_config.username,
        db_admin_password=hindsight_config.require("db_password"),
        db_host=hindsight_db.db_instance.address,
        role_statements={
            **postgres_role_statements,
            "app": {
                **postgres_role_statements["app"],
                "create": hindsight_app_create_statements,
            },
        },
    )
)

################################
# Vault and Kubernetes auth
################################
hindsight_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="hindsight",
        namespace=NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        vault_policy_path=Path(__file__).parent / "hindsight_policy.hcl",
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name=SERVICE_ACCOUNT_NAME,
        create_irsa_service_account=True,
        vault_sync_service_account_names=["hindsight-vault"],
        k8s_labels=k8s_app_labels,
    )
)

# creds/app mints a new username as well as a new password on every lease, so
# both are substituted into the URLs below, and rotation restarts the API.
hindsight_db_creds = OLVaultK8SSecret(
    f"hindsight-db-creds-{stack_info.env_suffix}",
    OLVaultK8SDynamicSecretConfig(
        name=DB_CREDS_SECRET_NAME,
        namespace=NAMESPACE,
        labels=k8s_labels,
        dest_secret_labels=k8s_labels,
        dest_secret_name=DB_CREDS_SECRET_NAME,
        mount=hindsight_db_vault_backend.db_mount.path,
        path="creds/app",
        exclude_raw=True,
        templates={
            "DB_USERNAME": '{{ get .Secrets "username" }}',
            "DB_PASSWORD": '{{ get .Secrets "password" }}',
        },
        restart_targets=[OLVaultRestartTarget(kind="Deployment", name=API_NAME)],
        vaultauth=hindsight_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[hindsight_auth_binding.vault_k8s_resources],
    ),
)

################################
# Embeddings server (Hugging Face text embeddings inference)
################################
embeddings_model = hindsight_config.require("embeddings_model")
embeddings_model_revision = hindsight_config.require("embeddings_model_revision")
embeddings_cpu = hindsight_config.require("embeddings_cpu")
embeddings_selector = {"app.kubernetes.io/name": EMBEDDINGS_NAME}
embeddings_labels = {
    **k8s_app_labels.workload_labels(alert_tier=alert_tier),
    **embeddings_selector,
}

embeddings_deployment = kubernetes.apps.v1.Deployment(
    f"hindsight-embeddings-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=EMBEDDINGS_NAME, namespace=NAMESPACE, labels=k8s_labels
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=hindsight_config.require_int("embeddings_replicas"),
        selector=kubernetes.meta.v1.LabelSelectorArgs(match_labels=embeddings_selector),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=embeddings_labels),
            spec=kubernetes.core.v1.PodSpecArgs(
                # The CPU image is published for amd64 only.
                node_selector={"kubernetes.io/arch": "amd64"},
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="text-embeddings-inference",
                        image=(
                            "ghcr.io/huggingface/text-embeddings-inference:"
                            f"cpu-{TEXT_EMBEDDINGS_INFERENCE_VERSION}"
                        ),
                        args=[
                            "--json-output",
                            "--model-id",
                            embeddings_model,
                            "--revision",
                            embeddings_model_revision,
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="PORT", value=str(EMBEDDINGS_PORT)
                            ),
                            # The image defaults to 8 threads whatever the
                            # container is given.
                            kubernetes.core.v1.EnvVarArgs(
                                name="RAYON_NUM_THREADS", value=embeddings_cpu
                            ),
                        ],
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                name="http", container_port=EMBEDDINGS_PORT
                            )
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": embeddings_cpu,
                                "memory": hindsight_config.require("embeddings_memory"),
                            },
                            limits={
                                "memory": hindsight_config.require("embeddings_memory")
                            },
                        ),
                        # The model is downloaded from Hugging Face on start.
                        startup_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/health", port="http"
                            ),
                            period_seconds=5,
                            failure_threshold=60,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/health", port="http"
                            ),
                            period_seconds=10,
                        ),
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/health", port="http"
                            ),
                            period_seconds=20,
                            failure_threshold=3,
                        ),
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="model-cache", mount_path=MODEL_CACHE_PATH
                            )
                        ],
                    )
                ],
                volumes=[
                    kubernetes.core.v1.VolumeArgs(
                        name="model-cache",
                        empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(
                            size_limit="2Gi"
                        ),
                    )
                ],
            ),
        ),
    ),
)

embeddings_service = kubernetes.core.v1.Service(
    f"hindsight-embeddings-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=EMBEDDINGS_NAME, namespace=NAMESPACE, labels=k8s_labels
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        selector=embeddings_selector,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="http", port=EMBEDDINGS_PORT, target_port="http"
            )
        ],
    ),
)

################################
# Hindsight API
################################
extension_source = EXTENSION_SOURCE.read_text()
# Auto-named, so an edit to the extension yields a new ConfigMap name, which
# changes the pod template and rolls the Deployment.
extension_config_map = kubernetes.core.v1.ConfigMap(
    f"hindsight-ol-memory-ext-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(namespace=NAMESPACE, labels=k8s_labels),
    data={"__init__.py": extension_source},
    opts=ResourceOptions(replace_on_changes=["data"], delete_before_replace=False),
)

db_url = Output.concat(
    "postgresql://$(DB_USERNAME):$(DB_PASSWORD)@",
    hindsight_db.db_instance.address,
    f":{DEFAULT_POSTGRES_PORT}/{DB_NAME}?sslmode=require",
)


def _client_map(clients: dict[str, str]) -> str:
    return ",".join(f"{client_id}={app}" for client_id, app in clients.items())


# Everything Hindsight reads is a HINDSIGHT_API_* setting. Values that are the
# same in every environment are here; the rest come from stack config.
hindsight_settings = {
    "HOST": "0.0.0.0",  # noqa: S104
    "PORT": str(API_PORT),
    "LOG_FORMAT": "json",
    # Storage and retrieval only. No provider means reflect and mental models
    # cannot run even if the validator let them through, and chunks-mode retain
    # stores the caller's text as given instead of extracting from it.
    "LLM_PROVIDER": "none",
    "RETAIN_EXTRACTION_MODE": "chunks",
    "ENABLE_OBSERVATIONS": "false",
    "ENABLE_AUTO_CONSOLIDATION": "false",
    # On by default: stores every LLM call's input and output, and bank deletion
    # does not remove it. Off, so a forget is complete.
    "LLM_TRACE_ENABLED": "false",
    # Chunks are what recall returns; the full document text would be a second
    # copy of every note for no reader.
    "STORE_DOCUMENT_TEXT": "false",
    # The default (0) builds three HNSW indexes per bank with a blocking CREATE
    # INDEX, which with a bank per learner is thousands of indexes. Below this
    # many rows a bank is scanned instead.
    "VECTOR_INDEX_MIN_ROWS": "10000",
    # Not part of the supported interface.
    "MCP_ENABLED": "false",
    # A retain with attachments stores the file bytes in a table with no bank_id,
    # out of the fence trigger's reach. Notes are text.
    "ENABLE_FILE_UPLOAD_API": "false",
    "EMBEDDINGS_PROVIDER": "tei",
    "EMBEDDINGS_TEI_URL": (
        f"http://{EMBEDDINGS_NAME}.{NAMESPACE}.svc.cluster.local:{EMBEDDINGS_PORT}"
    ),
    "RERANKER_PROVIDER": "rrf",
    "EXTENSION_PASSTHROUGH_HEADERS": "x-ol-learner",
    "TENANT_EXTENSION": f"{EXTENSION_PACKAGE}:KeycloakTenantExtension",
    "TENANT_ISSUER": keycloak_issuer,
    "TENANT_JWKS_URL": f"{keycloak_issuer}/protocol/openid-connect/certs",
    "TENANT_AUDIENCE": hindsight_config.require("keycloak_audience"),
    "TENANT_CLIENTS": _client_map(hindsight_config.require_object("app_clients")),
    "TENANT_ADMIN_CLIENTS": ",".join(hindsight_config.require_object("admin_clients")),
    "TENANT_GENERAL_SCOPE": hindsight_config.require("general_scope"),
    "OPERATION_VALIDATOR_EXTENSION": f"{EXTENSION_PACKAGE}:LearnerBankValidator",
    "MEMORY_DEFENSE_EXTENSION": f"{EXTENSION_PACKAGE}:OLMemoryDefense",
}

api_selector = {"app.kubernetes.io/name": API_NAME}
api_labels = {
    **k8s_app_labels.workload_labels(alert_tier=alert_tier, component=Component.api),
    **api_selector,
}

api_deployment = kubernetes.apps.v1.Deployment(
    f"hindsight-api-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=API_NAME, namespace=NAMESPACE, labels=k8s_labels
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        # Startup migrations take an advisory lock, so replicas can start
        # together; each pod's worker claims operations under its own id.
        replicas=hindsight_config.require_int("api_replicas"),
        selector=kubernetes.meta.v1.LabelSelectorArgs(match_labels=api_selector),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=api_labels,
                annotations={
                    "ol.mit.edu/extension-hash": hashlib.sha256(
                        extension_source.encode()
                    ).hexdigest()
                },
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name=SERVICE_ACCOUNT_NAME,
                security_context=kubernetes.core.v1.PodSecurityContextArgs(
                    run_as_non_root=True, run_as_user=1000, run_as_group=1000
                ),
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="hindsight-api",
                        image=f"ghcr.io/vectorize-io/hindsight-api:{HINDSIGHT_VERSION}",
                        env_from=[
                            kubernetes.core.v1.EnvFromSourceArgs(
                                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                    name=DB_CREDS_SECRET_NAME
                                )
                            )
                        ],
                        env=[
                            # Each worker claims queued operations under its id.
                            kubernetes.core.v1.EnvVarArgs(
                                name="HINDSIGHT_API_WORKER_ID",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    field_ref=kubernetes.core.v1.ObjectFieldSelectorArgs(
                                        field_path="metadata.name"
                                    )
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="PYTHONPATH", value=EXTENSION_ROOT
                            ),
                            # $(VAR) substitution only sees variables defined
                            # before it, and envFrom counts as before.
                            kubernetes.core.v1.EnvVarArgs(
                                name="HINDSIGHT_API_DATABASE_URL", value=db_url
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="HINDSIGHT_API_OPERATION_VALIDATOR_DATABASE_URL",
                                value=db_url,
                            ),
                            *(
                                kubernetes.core.v1.EnvVarArgs(
                                    name=f"HINDSIGHT_API_{name}", value=value
                                )
                                for name, value in hindsight_settings.items()
                            ),
                        ],
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                name="http", container_port=API_PORT
                            )
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": hindsight_config.require("api_cpu"),
                                "memory": hindsight_config.require("api_memory"),
                            },
                            limits={"memory": hindsight_config.require("api_memory")},
                        ),
                        # Startup runs the migrations and probes the embeddings
                        # server before the API answers.
                        startup_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/health", port="http"
                            ),
                            period_seconds=5,
                            failure_threshold=60,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/health", port="http"
                            ),
                            period_seconds=10,
                        ),
                        # /health checks the database too; a database blip should
                        # take the pod out of the Service, not restart it.
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/health/live", port="http"
                            ),
                            period_seconds=20,
                            failure_threshold=3,
                        ),
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="ol-memory-ext",
                                mount_path=f"{EXTENSION_ROOT}/{EXTENSION_PACKAGE}",
                                read_only=True,
                            )
                        ],
                    )
                ],
                volumes=[
                    kubernetes.core.v1.VolumeArgs(
                        name="ol-memory-ext",
                        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                            name=extension_config_map.metadata.name
                        ),
                    )
                ],
            ),
        ),
    ),
    opts=ResourceOptions(depends_on=[hindsight_db_creds, embeddings_service]),
)

api_service = kubernetes.core.v1.Service(
    f"hindsight-api-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=API_NAME, namespace=NAMESPACE, labels=k8s_labels
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        selector=api_selector,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="http", port=API_PORT, target_port="http"
            )
        ],
    ),
)

kubernetes.apiextensions.CustomResource(
    f"hindsight-api-pod-monitor-{stack_info.env_suffix}",
    api_version="monitoring.coreos.com/v1",
    kind="PodMonitor",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=API_NAME, namespace=NAMESPACE, labels=k8s_labels
    ),
    spec={
        "selector": {"matchLabels": api_selector},
        "namespaceSelector": {"matchNames": [NAMESPACE]},
        "podMetricsEndpoints": [
            {"port": "http", "path": "/metrics", "scheme": "http", "interval": "60s"}
        ],
    },
)

export(
    "hindsight",
    {
        "url": f"http://{API_NAME}.{NAMESPACE}.svc.cluster.local:{API_PORT}",
        "keycloak_audience": hindsight_config.require("keycloak_audience"),
        "general_scope": hindsight_config.require("general_scope"),
        "db_address": hindsight_db.db_instance.address,
    },
)
