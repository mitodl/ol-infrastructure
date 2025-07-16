# ruff: noqa: F841, E501, S105, PLR0913, FIX002
import textwrap

import pulumi_aws as aws
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, StackReference, export

from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.aws.database import OLAmazonDB
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def create_k8s_resources(
    stack_info: StackInfo,
    edxapp_config: Config,
    network_stack: StackReference,
    edxapp_db: OLAmazonDB,
    edxapp_cache: OLAmazonCache,
    aws_config: AWSBase,
    vault_config: Config,
    vault_policy: vault.Policy,
):
    env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
    lms_deployment_name = f"{env_name}-edxapp-lms"
    cms_deployment_name = f"{env_name}-edxapp-cms"

    # Get various VPC / network configuration information
    apps_vpc = network_stack.require_output("applications_vpc")
    data_vpc = network_stack.require_output("data_vpc")
    operations_vpc = network_stack.require_output("operations_vpc")
    edxapp_target_vpc = (
        edxapp_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
    )
    edxapp_vpc = network_stack.require_output(edxapp_target_vpc)

    # TODO(Mike): Will require special handling for residential clusters
    k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]

    # Verify that the namespace exists in the EKS cluster
    cluster_stack = StackReference(
        f"infrastructure.aws.eks.applications.{stack_info.name}"
    )  # TODO(Mike): add logic for the residential clusters
    namespace = f"{stack_info.env_prefix}-openedx"
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(namespace, ns)
    )

    # Look up what what release to deploy for this stack
    release_info = OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)

    # Configure reusable global labels
    k8s_global_labels = K8sGlobalLabels(
        service=Services.edxapp,
        ou=BusinessUnit(stack_info.env_prefix),
        stack=stack_info,
    ).model_dump()

    # Create a kubernetes vault auth backend role
    edxapp_k8s_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"ol-{stack_info.env_prefix}-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name=f"{stack_info.env_prefix}-edxapp",
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=[namespace],
        token_policies=[vault_policy.name],
    )

    # Setup all the bits and bobs needed to interact with vault from k8s
    vault_k8s_resources = OLVaultK8SResources(
        resource_config=OLVaultK8SResourcesConfig(
            application_name=f"{stack_info.env_prefix}-edxapp",
            namespace=namespace,
            labels=k8s_global_labels,
            vault_address=vault_config.require("address"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            vault_auth_role_name=edxapp_k8s_vault_auth_backend_role.role_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[edxapp_k8s_vault_auth_backend_role],
        ),
    )

    # The edxapp application in Kubernetes requires its own security group
    edxapp_k8s_app_security_group = aws.ec2.SecurityGroup(
        f"ol-{stack_info.env_prefix}-edxapp-k8s-app-security-group-{stack_info.env_suffix}",
        name_prefix=f"{env_name}",
        description="Security group for the edxapp application in Kubernetes",
        egress=default_psg_egress_args,
        ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
        tags=aws_config.tags,
        vpc_id=apps_vpc["id"],
    )
    export("edxapp_k8s_app_security_group_id", edxapp_k8s_app_security_group.id)

    # Load the database configuration into a secret for the edxapp application
    db_creds_secret_name = "00-database-credentials-yaml"  # pragma: allowlist secret
    db_creds_secret = Output.all(
        address=edxapp_db.db_instance.address,
        port=edxapp_db.db_instance.port,
    ).apply(
        lambda db: OLVaultK8SSecret(
            f"ol-{stack_info.env_prefix}-edxapp-db-creds-secret-{stack_info.env_suffix}",
            OLVaultK8SDynamicSecretConfig(
                name="edxapp-db-creds",
                namespace=namespace,
                dest_secret_labels=k8s_global_labels,
                dest_secret_name=db_creds_secret_name,
                labels=k8s_global_labels,
                mount=f"mariadb-{stack_info.env_prefix}",
                path="creds/edxapp",
                templates={
                    "00-database-credentials.yaml": textwrap.dedent(f"""
                        mysql_creds: &mysql_creds
                          ENGINE: django.db.backends.mysql
                          HOST: {db["address"]}
                          PORT: {db["port"]}
                          USER: {{{{ get .Secrets "username" }}}}
                          PASSWORD: {{{{ get .Secrets "password" }}}}
                    """)
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
        ),
    )

    # Load the database configuration into a secret for the edxapp application
    db_connections_secret_name = (
        "01-database-connections-yaml"  # pragma: allowlist secret
    )
    db_connections_secret = Output.all(
        address=edxapp_db.db_instance.address,
        port=edxapp_db.db_instance.port,
    ).apply(
        lambda db: OLVaultK8SSecret(
            f"ol-{stack_info.env_prefix}-edxapp-db-connections-secret-{stack_info.env_suffix}",
            OLVaultK8SDynamicSecretConfig(
                name="edxapp-db-connections",
                namespace=namespace,
                dest_secret_labels=k8s_global_labels,
                dest_secret_name=db_connections_secret_name,
                labels=k8s_global_labels,
                mount=f"mariadb-{stack_info.env_prefix}",
                path="creds/edxapp-csmh",
                templates={
                    "01-database-connections.yaml": textwrap.dedent(f"""
                    DATABASES:
                      default:
                        ATOMIC_REQUESTS: true
                        CONN_MAX_AGE: 0
                        NAME: edxapp
                        OPTIONS:
                          charset: utf8mb4
                        <<: *mysql_creds
                      read_replica:
                        CONN_MAX_AGE: 0
                        NAME: edxapp
                        OPTIONS:
                          charset: utf8mb4
                        <<: *mysql_creds
                      student_module_history:
                        CONN_MAX_AGE: 0
                        ENGINE: django.db.backends.mysql
                        HOST: {db["address"]}
                        PORT: {db["port"]}
                        NAME: edxapp_csmh
                        OPTIONS:
                          charset: utf8mb4
                        USER: {{{{ get .Secrets "username" }}}}
                        PASSWORD: {{{{ get .Secrets "password" }}}}
                    """)
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
        ),
    )

    # Load the MongoDB configuration into a secret for the edxapp application
    mongo_db_creds_secret_name = (
        "02-mongodb-credentials-yaml"  # pragma: allowlist secret
    )
    mongo_db_creds_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-mongo-db-creds-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name="edxapp-mongo-db-creds",
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=mongo_db_creds_secret_name,
            labels=k8s_global_labels,
            mount=f"secret-{stack_info.env_prefix}",
            mount_type="kv-v1",
            path="mongodb-edxapp",
            templates={
                "02-mongo-db-credentials.yaml": textwrap.dedent("""
                    mongodb_creds: &mongodb_creds
                      authsource: admin
                      host: null # TODO
                      port: 27017
                      db: edxapp
                      replicaSet: null # TODO
                      user: {{ get .Secrets "username" }}
                      password: {{ get .Secrets "password" }}
                      ssl: # TODO
                """),
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
    )

    # Load the MongoDB forum configuration into a secret for the edxapp application
    mongo_db_forum_secret_name = (
        "03-mongodb-forum-credentials-yaml"  # pragma: allowlist secret
    )
    mongo_db_forum_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-mongo-forum-creds-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name="edxapp-mongo-forum-creds",
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=mongo_db_forum_secret_name,
            labels=k8s_global_labels,
            mount=f"secret-{stack_info.env_prefix}",
            mount_type="kv-v1",
            path="mongodb-forum",
            templates={
                "03-mongo-db-forum-credentials.yaml": textwrap.dedent("""
                    FORUM_MONGODB_CLIENT_PARAMETERS:
                      authSource: admin
                      host:  # TODO
                      port: 27017
                      # db is missing here, that is the difference from above
                      replicaSet: # TODO
                      user: {{ get .Secrets "username" }}
                      password: {{ get .Secrets "password" }}
                      ssl: # TODO
                """),
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
    )

    # Load the Redis configuration and other generic secrets into a secret for the edxapp application
    general_secrets_name = "10-general-secrets-yaml"  # pragma: allowlist secret
    general_secrets_secret = Output.all(
        redis_hostname=edxapp_cache.address,
    ).apply(
        lambda redis_cache: OLVaultK8SSecret(
            f"ol-{stack_info.env_prefix}-edxapp-general-secret-{stack_info.env_suffix}",
            OLVaultK8SStaticSecretConfig(
                name="edxapp-general-secrets",
                namespace=namespace,
                dest_secret_labels=k8s_global_labels,
                dest_secret_name=general_secrets_name,
                labels=k8s_global_labels,
                mount=f"secret-{stack_info.env_prefix}",
                mount_type="kv-v1",
                path="edxapp",
                templates={
                    "10-general-secrets.yaml": textwrap.dedent(f"""
                        CELERY_BROKER_PASSWORD: {{{{ get .Secrets "redis_auth_token" }}}}
                        FERNET_KEYS: {{{{ get .Secrets "fernet_keys" }}}}
                        redis_cache_config: &redis_cache_config
                          BACKEND: django_redis.cache.RedisCache
                          LOCATION: rediss://default@{redis_cache["redis_hostname"]}:6379/0
                          KEY_FUNCTION: common.djangoapps.util.memcache.safe_key
                          OPTIONS:
                            CLIENT_CLASS: django_redis.client.DefaultClient
                            PASSWORD: {{{{ get .Secrets "redis_auth_token" }}}}
                        SECRET_KEY: {{{{ get .Secrets "django_secret_key" }}}}
                        JWT_AUTH:  # NEEDS ATTENTION
                          JWT_ALGORITHM: HS256
                          JWT_AUDIENCE: mitxonline
                          JWT_AUTH_COOKIE: {stack_info.env_prefix}-{stack_info.env_suffix}-edx-jwt-cookie
                          JWT_AUTH_COOKIE_HEADER_PAYLOAD: {stack_info.env_prefix}-{stack_info.env_suffix}-edx-jwt-cookie-header-payload
                          JWT_AUTH_COOKIE_SIGNATURE: {stack_info.env_prefix}-{stack_info.env_suffix}-edx-jwt-cookie-signature
                          JWT_ISSUER: 'https://{edxapp_config.require_object("domains")["lms"]}/oauth2'
                          JWT_LOGIN_CLIENT_ID: login-service-client-id
                          JWT_LOGIN_SERVICE_USERNAME: login_service_user
                          JWT_PRIVATE_SIGNING_JWK: '{{{{ get .Secrets "private_signing_jwk" }}}}'
                          JWT_PUBLIC_SIGNING_JWK_SET: '{{{{ get .Secrets "public_signing_jwk" }}}}'
                          JWT_SECRET_KEY: {{{{ get .Secrets "django_secret_key" }}}}
                          JWT_SIGNING_ALGORITHM: RS512
                          JWT_ISSUERS:
                            - ISSUER: https://{edxapp_config.require_object("domains")["lms"]}/oauth2
                              AUDIENCE: mitxonline
                              SECRET_KEY: {{{{ get .Secrets "django_secret_key" }}}}
                        OPENAI_SECRET_KEY: {{{{ get .Secrets "openai_api_key" }}}}
                        OPENAI_API_KEY: {{{{ get .Secrets "openai_api_key" }}}}
                        RETIRED_USER_SALTS: {{{{ get .Secrets "user_retirement_salts" }}}}
                        SENTRY_DSN: {{{{ get .Secrets "sentry_dsn" }}}}
                        SYSADMIN_GITHUB_WEBHOOK_KEY: {{{{ get .Secrets "sysadmin_git_webhook_secret" }}}}
                        PROCTORING_BACKENDS:
                          DEFAULT: 'proctortrack'
                          'proctortrack':
                            'client_id': '{{{{ get .Secrets "proctortrack_client_id" }}}}'
                            'client_secret': '{{{{ get .Secrets "proctortrack_client_secret" }}}}'
                            'base_url': '{{{{ get .Secrets "edxapp/proctortrack-base-url" }}}}'
                          'null': {{}}
                        PROCTORING_USER_OBFUSCATION_KEY: {{{{ get .Secrets "proctortrack_user_obfuscation_key" }}}}
                    """),
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
        ),
    )

    # Load the xqueue secrets into a secret for the edxapp application
    xqueue_secret_name = "11-xqueue-secrets-yaml"  # pragma: allowlist secret
    xqueue_secret_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-xqueue-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name="edxapp-xqueue-secrets",
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=xqueue_secret_name,
            labels=k8s_global_labels,
            mount=f"secret-{stack_info.env_prefix}",
            mount_type="kv-v1",
            path="edx-xqueue",
            templates={
                "11-xqueue-secrets.yaml": textwrap.dedent("""
                        XQUEUE_INTERFACE:
                          django_auth:
                            password: {{ get .Secrets "edxapp_password" }}
                            username: edxapp
                          url: http://xqueue.service.consul:8040 # TODO
                    """),
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
    )

    # Load the forum secrets into a secret for the edxapp application
    forum_secret_name = "12-forum-secrets-yaml"  # pragma: allowlist secret
    forum_secret_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-forum-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name="edxapp-forum-secrets",
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=forum_secret_name,
            labels=k8s_global_labels,
            mount=f"secret-{stack_info.env_prefix}",
            mount_type="kv-v1",
            path="edx-forum",
            templates={
                "12-forum-secrets.yaml": textwrap.dedent("""
                        COMMENTS_SERVICE_KEY: {{ get .Secrets "forum_api_key" }}
                    """),
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
    )

    return {
        "edxapp_k8s_app_security_group_id": edxapp_k8s_app_security_group.id,
    }
