# ruff: noqa: F841, E501, S105, PLR0913, FIX002, PLR0915, FBT003
import textwrap
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
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
    cluster_stack: StackReference,
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

    ############################################
    # Shared configuration for BOTH LMS and CMS
    ############################################
    # Load the database configuration into a secret for the edxapp application
    db_creds_secret_name = "00-database-credentials-yaml"  # pragma: allowlist secret
    db_creds_secret = Output.all(
        address=edxapp_db.db_instance.address,
        port=edxapp_db.db_instance.port,
    ).apply(
        lambda db: OLVaultK8SSecret(
            f"ol-{stack_info.env_prefix}-edxapp-db-creds-secret-{stack_info.env_suffix}",
            OLVaultK8SDynamicSecretConfig(
                name=db_creds_secret_name,
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
            opts=ResourceOptions(delete_before_replace=True),
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
                name=db_connections_secret_name,
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
            opts=ResourceOptions(delete_before_replace=True),
        ),
    )

    # Load the MongoDB configuration into a secret for the edxapp application
    mongo_db_creds_secret_name = (
        "02-mongodb-credentials-yaml"  # pragma: allowlist secret
    )
    mongo_db_creds_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-mongo-db-creds-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=mongo_db_creds_secret_name,
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
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Load the MongoDB forum configuration into a secret for the edxapp application
    mongo_db_forum_secret_name = (
        "03-mongodb-forum-credentials-yaml"  # pragma: allowlist secret
    )
    mongo_db_forum_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-mongo-forum-creds-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=mongo_db_forum_secret_name,
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
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Load the Redis configuration and other generic secrets into a secret for the edxapp application
    general_secrets_name = "10-general-secrets-yaml"  # pragma: allowlist secret
    general_secrets_secret = Output.all(
        redis_hostname=edxapp_cache.address,
    ).apply(
        lambda redis_cache: OLVaultK8SSecret(
            f"ol-{stack_info.env_prefix}-edxapp-general-secret-{stack_info.env_suffix}",
            OLVaultK8SStaticSecretConfig(
                name=general_secrets_name,
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
            opts=ResourceOptions(delete_before_replace=True),
        ),
    )

    # Load the xqueue secrets into a secret for the edxapp application
    xqueue_secret_name = "11-xqueue-secrets-yaml"  # pragma: allowlist secret
    xqueue_secret_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-xqueue-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=xqueue_secret_name,
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
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Load the forum secrets into a secret for the edxapp application
    forum_secret_name = "12-forum-secrets-yaml"  # pragma: allowlist secret
    forum_secret_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-forum-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=forum_secret_name,
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
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Load general configuration from a file into a configmap
    general_config_name = "50-general-config-yaml"
    general_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-general-config-{stack_info.env_suffix}",
        metadata={
            "name": general_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "50-general-config.yaml": Path(
                f"files/edxapp/{stack_info.env_prefix}/50-general-config.yaml"
            ).read_text()
        },
    )

    # Misc values needed for the next step
    course_bucket_name = f"{env_name}-edxapp-courses"
    grades_bucket_name = f"{env_name}-edxapp-grades"
    storage_bucket_name = f"{env_name}-edxapp-storage"
    ses_configuration_set = f"edxapp-{env_name}"
    opensearch_stack = StackReference(
        f"infrastructure.aws.opensearch.{stack_info.env_prefix}.{stack_info.name}"
    )

    # Load environment specific configuration directly from code into a configmap
    interpolated_config_name = "60-interpolated-config-yaml"
    interpolated_config_map = Output.all(
        redis_hostname=edxapp_cache.address,
        opensearch_hostname=opensearch_stack.require_output("cluster")["endpoint"],
    ).apply(
        lambda runtime_config: kubernetes.core.v1.ConfigMap(
            f"ol-{stack_info.env_prefix}-edxapp-interpolated-config-{stack_info.env_suffix}",
            metadata={
                "name": interpolated_config_name,
                "namespace": namespace,
                "labels": k8s_global_labels,
            },
            data={
                "60-interpolated-config.yaml": textwrap.dedent(f"""
                    ALLOWED_HOSTS:
                    - {edxapp_config.require_object("domains")["lms"]}
                    - {edxapp_config.require_object("domains")["preview"]}
                    - {edxapp_config.require_object("domains")["studio"]}
                    AWS_S3_CUSTOM_DOMAIN: {storage_bucket_name}.s3.amazonaws.com
                    AWS_STORAGE_BUCKET_NAME: {storage_bucket_name}
                    AWS_SES_CONFIGURATION_SET: {ses_configuration_set}
                    BASE_COOKIE_DOMAIN: {edxapp_config.require_object("domains")["lms"]}
                    BLOCK_STRUCTURES_SETTINGS:
                      COURSE_PUBLISH_TASK_DELAY: 30
                      PRUNING_ACTIVE: true  # MODIFIED
                      TASK_DEFAULT_RETRY_DELAY: 30
                      TASK_MAX_RETRIES: 5
                      STORAGE_CLASS: storages.backends.s3boto3.S3Boto3Storage
                      DIRECTORY_PREFIX: coursestructure/
                      STORAGE_KWARGS:
                        bucket_name: {storage_bucket_name}
                        default_acl: public-read
                    EMAIL_USE_COURSE_ID_FROM_FOR_BULK: {edxapp_config.get_bool("email_use_course_id_from_for_bulk", False)}
                    BULK_EMAIL_DEFAULT_FROM_EMAIL: {edxapp_config.get("bulk_email_default_from_email") or edxapp_config.require("sender_email_address")}
                    CELERY_BROKER_HOSTNAME: {runtime_config["redis_hostname"]}
                    CMS_BASE: {edxapp_config.require_object("domains")["studio"]}
                    CONTACT_EMAIL: {edxapp_config.require("sender_email_address")}
                    CORS_ORIGIN_WHITELIST:
                    - https://{edxapp_config.require_object("domains")["lms"]}
                    - https://{edxapp_config.require_object("domains")["studio"]}
                    - https://{edxapp_config.require_object("domains")["preview"]}
                    - https://{edxapp_config.require("marketing_domain")}
                    # - https://{{{{ key "edx/notes-api-host" }}}} # TODO
                    # - https://{{{{ key "edxapp/learn-ai-frontend-domain" }}}} # TODO
                    COURSE_IMPORT_EXPORT_BUCKET: {storage_bucket_name}
                    CROSS_DOMAIN_CSRF_COOKIE_DOMAIN: {edxapp_config.require_object("domains")["lms"]}
                    CROSS_DOMAIN_CSRF_COOKIE_NAME: {env_name}-edxapp-csrftoken
                    CSRF_TRUSTED_ORIGINS:  # MODIFIED
                    - https://{edxapp_config.require_object("domains")["lms"]}
                    DEFAULT_FEEDBACK_EMAIL: {edxapp_config.require("sender_email_address")}
                    DEFAULT_FROM_EMAIL: {edxapp_config.require("sender_email_address")}
                    DISCUSSIONS_MICROFRONTEND_URL: https://{{ key "edxapp/lms-domain" }}/discuss
                    EDXMKTG_USER_INFO_COOKIE_NAME: {env_name}-edx-user-info
                    # EDXNOTES_INTERNAL_API: https://{{ key "edx/notes-api-host" }}/api/v1  # TODO
                    # EDXNOTES_PUBLIC_API: https://{{ key "edx/notes-api-host" }}/api/v1  # TODO
                    ELASTIC_SEARCH_CONFIG:
                    - host: {runtime_config["opensearch_hostname"]}
                      port: 443
                      use_ssl: true
                    ELASTIC_SEARCH_CONFIG_ES7:
                    - host: {runtime_config["opensearch_hostname"]}
                      port: 443
                      use_ssl: true
                    FILE_UPLOAD_STORAGE_BUCKET_NAME: {storage_bucket_name}
                    FORUM_ELASTIC_SEARCH_CONFIG:
                    - host: {runtime_config["opensearch_hostname"]}
                      port: "443"
                      use_ssl: true
                    FORUM_MONGODB_DATABASE: "forum"
                    GITHUB_REPO_ROOT: /openedx/data
                    GOOGLE_ANALYTICS_ACCOUNT: {edxapp_config.require("google_analytics_id")}
                    GRADES_DOWNLOAD:
                      BUCKET: {grades_bucket_name}  # MODIFIED
                      ROOT_PATH: grades  # MODIFIED
                      STORAGE_CLASS: django.core.files.storage.S3Storage  # MODIFIED
                      STORAGE_KWARGS:
                        location: grades/
                      STORAGE_TYPE: S3  # MODIFIED
                    IDA_LOGOUT_URI_LIST:
                    - https://{edxapp_config.require("marketing_domain")}/logout
                    - https://{edxapp_config.require_object("domains")["studio"]}/logout
                    - https://{{ key "edxapp/learn-api-domain" }}/logout  # TODO
                    LANGUAGE_COOKIE: {env_name}-openedx-language-preference
                    # MIT_LEARN_AI_API_URL: https://{{ key "edxapp/learn-api-domain" }}/ai  # Added for ol_openedx_chat  # TODO
                    # MIT_LEARN_API_BASE_URL: https://{{ key "edxapp/learn-api-domain" }}/learn  # Added for ol_openedx_chat  # TODO
                    # MIT_LEARN_SUMMARY_FLASHCARD_URL: https://{{ key "edxapp/learn-api-domain" }}/learn/api/v1/contentfiles/  # Added for ol_openedx_chat  # TODO
                    # MIT_LEARN_BASE_URL: https://{{ key "edxapp/learn-frontend-domain" }}  # TODO
                    MIT_LEARN_LOGO: https://{edxapp_config.require_object("domains")["lms"]}/static/mitxonline/images/mit-learn-logo.svg
                    LEARNING_MICROFRONTEND_URL: https://{edxapp_config.require_object("domains")["lms"]}/learn
                    LMS_BASE: {edxapp_config.require_object("domains")["lms"]}
                    LMS_INTERNAL_ROOT_URL: https://{edxapp_config.require_object("domains")["lms"]}
                    LMS_ROOT_URL: https://{edxapp_config.require_object("domains")["lms"]}
                    LOGIN_REDIRECT_WHITELIST:  # MODIFIED
                    - {edxapp_config.require_object("domains")["studio"]}
                    - {edxapp_config.require_object("domains")["lms"]}
                    - {edxapp_config.require_object("domains")["preview"]}
                    - {edxapp_config.require("marketing_domain")}
                    LOGO_URL: https://{edxapp_config.require_object("domains")["lms"]}/static/mitxonline/images/logo.svg
                    LOGO_URL_PNG_FOR_EMAIL: https://{edxapp_config.require_object("domains")["lms"]}/static/mitxonline/images/logo.png
                    LOGO_TRADEMARK_URL: https://{edxapp_config.require_object("domains")["lms"]}/static/mitxonline/images/mit-logo.svg
                    MARKETING_SITE_BASE_URL: https://{edxapp_config.require("marketing_domain")}/ # ADDED - to support mitxonline-theme
                    MARKETING_SITE_CHECKOUT_URL: https://{edxapp_config.require("marketing_domain")}/cart/add/ # ADDED - to support mitxonline checkout
                    MKTG_URLS:
                      ROOT: https://{edxapp_config.require("marketing_domain")}/
                    MKTG_URL_OVERRIDES:
                      COURSES: https://{edxapp_config.require("marketing_domain")}/
                      PRIVACY: https://{edxapp_config.require("marketing_domain")}/privacy-policy/
                      TOS: https://{edxapp_config.require("marketing_domain")}/terms-of-service/
                      ABOUT: https://{edxapp_config.require("marketing_domain")}/about-us/
                      HONOR: https://{edxapp_config.require("marketing_domain")}/honor-code/
                      ACCESSIBILITY: https://accessibility.mit.edu/
                      CONTACT: https://mitxonline.zendesk.com/hc/en-us/requests/new/
                      TOS_AND_HONOR: ''
                    NOTIFICATIONS_DEFAULT_FROM_EMAIL: {edxapp_config.require("bulk_email_default_from_email") or edxapp_config.require("sender_email_address")}
                    PAYMENT_SUPPORT_EMAIL: {edxapp_config.require("sender_email_address")}
                    SENTRY_ENVIRONMENT: {env_name}
                    # Removing the session cookie domain as it is no longer needed for sharing the cookie
                    # between LMS and Studio (TMM 2021-10-22)
                    # UPDATE: The session cookie domain appears to still be required for enabling the
                    # preview subdomain to share authentication with LMS (TMM 2021-12-20)
                    SESSION_COOKIE_DOMAIN: {".{}".format(edxapp_config.require_object("domains")["lms"].split(".", 1)[-1])}
                    UNIVERSITY_EMAIL: {edxapp_config.require("sender_email_address")}
                    ECOMMERCE_PUBLIC_URL_ROOT: https://{edxapp_config.require_object("domains")["lms"]}
            """),
            },
            opts=ResourceOptions(delete_before_replace=True),
        )
    )

    ############################################
    # Configuration for JUST the CMS containers
    ############################################

    # A secret for JUST CMS containers, not LMS.
    cms_oauth_secret_name = "70-cms-oauth-credentials-yaml"  # pragma: allowlist secret
    cms_oauth_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-cms-oauth-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=cms_oauth_secret_name,
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=cms_oauth_secret_name,
            labels=k8s_global_labels,
            mount=f"secret-{stack_info.env_prefix}",
            mount_type="kv-v1",
            path="edxapp",
            templates={
                "70-cms-oauth-credentials.yaml": textwrap.dedent("""
                    SOCIAL_AUTH_EDX_OAUTH2_KEY: {{ (get .Secrets "studio_oauth_client").id }}
                    SOCIAL_AUTH_EDX_OAUTH2_SECRET: {{ (get .Secrets "studio_oauth_client").secret }}
                """),
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(delete_before_replace=True),
    )

    # General configuration items for the CMS application.
    cms_general_config_name = "71-cms-general-config-yaml"
    cms_general_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-cms-general-config-{stack_info.env_suffix}",
        metadata={
            "name": cms_general_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "71-cms-general-config.yaml": Path(
                f"files/edxapp/{stack_info.env_prefix}/71-cms-general-config.yaml"
            ).read_text()
        },
    )

    # Interpolated configuration items for the CMS application.
    cms_interpolated_config_name = "72-cms-interpolated-config-yaml"
    cms_interpolated_config = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-cms-interpolated-config-{stack_info.env_suffix}",
        metadata={
            "name": cms_interpolated_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "72-cms-interpolated-config.yaml": textwrap.dedent(f"""
                SITE_NAME: {edxapp_config.require_object("domains")["studio"]}
                SOCIAL_AUTH_EDX_OAUTH2_URL_ROOT: https://{edxapp_config.require_object("domains")["lms"]}
                SOCIAL_AUTH_EDX_OAUTH2_PUBLIC_URL_ROOT: https://{edxapp_config.require_object("domains")["lms"]}
                SESSION_COOKIE_NAME: {env_name}-edx-studio-sessionid
            """)
        },
        opts=ResourceOptions(delete_before_replace=True),
    )

    ############################################
    # Configuration for JUST the LMS containers
    ############################################

    # A secret for JUST CMS containers, not LMS.
    lms_oauth_secret_name = "80-lms-oauth-credentials-yaml"  # pragma: allowlist secret
    lms_oauth_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-lms-oauth-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=lms_oauth_secret_name,
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=lms_oauth_secret_name,
            labels=k8s_global_labels,
            mount=f"secret-{stack_info.env_prefix}",
            mount_type="kv-v1",
            path="edxapp",
            templates={
                "80-lms-oauth-credentials.yaml": textwrap.dedent("""
                    SOCIAL_AUTH_OAUTH_SECRETS:
                        ol-oauth2: {{ get .Secrets "mitxonline_oauth_secret" }}
                """),
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Interpolated configuration items for the CMS application.
    lms_interpolated_config_name = "82-lms-interpolated-config-yaml"
    lms_interpolated_config = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-lms-interpolated-config-{stack_info.env_suffix}",
        metadata={
            "name": lms_interpolated_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "82-lms-interpolated-config.yaml": textwrap.dedent(f"""
                SITE_NAME: {edxapp_config.require_object("domains")["lms"]}
                SESSION_COOKIE_NAME: {env_name}-edx-lms-sessionid
            """)
        },
        opts=ResourceOptions(delete_before_replace=True),
    )

    ############################################
    # uwsgi.ini
    ############################################
    uwsgi_ini_config_name = "uwsgi-ini"
    uwsgi_ini_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-uwsgi-ini-config-{stack_info.env_suffix}",
        metadata={
            "name": uwsgi_ini_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={"uwsgi.ini": Path("files/edxapp/uwsgi.ini").read_text()},
    )

    ############################################
    # cms deployment resources
    ############################################
    # All of the secrets and configmaps that will be mounted into the edxapp cms containers
    # The names are prefixed with numbers to control the order they are concatenated in.
    cms_edxapp_config_sources = {
        db_creds_secret_name: db_creds_secret,
        db_connections_secret_name: db_connections_secret,
        mongo_db_creds_secret_name: mongo_db_creds_secret,
        mongo_db_forum_secret_name: mongo_db_forum_secret,
        general_secrets_name: general_secrets_secret,
        xqueue_secret_name: xqueue_secret_secret,
        forum_secret_name: forum_secret_secret,
        general_config_name: general_config_map,
        interpolated_config_name: interpolated_config_map,
        # Just CMS specific resources below this line
        cms_oauth_secret_name: cms_oauth_secret,
        cms_general_config_name: cms_general_config_map,
        cms_interpolated_config_name: cms_interpolated_config,
    }
    cms_edxapp_secret_names = [
        db_creds_secret_name,
        db_connections_secret_name,
        mongo_db_creds_secret_name,
        mongo_db_forum_secret_name,
        general_secrets_name,
        xqueue_secret_name,
        forum_secret_name,
        # Just CMS specific resources below this line
        cms_oauth_secret_name,
    ]
    cms_edxapp_configmap_names = [
        general_config_name,
        interpolated_config_name,
        # Just CMS specific resources below this line
        cms_general_config_name,
        cms_interpolated_config_name,
    ]

    # Define the volumes that will be mounted into the edxapp containers
    cms_edxapp_volumes = [
        kubernetes.core.v1.VolumeArgs(
            name=secret_name,
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(secret_name=secret_name),
        )
        for secret_name in cms_edxapp_secret_names
    ]
    cms_edxapp_volumes.extend(
        [
            kubernetes.core.v1.VolumeArgs(
                name=configmap_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmap_name
                ),
            )
            for configmap_name in cms_edxapp_configmap_names
        ]
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="edxapp-config",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name=uwsgi_ini_config_name,
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=uwsgi_ini_config_name
            ),
        )
    )

    # Define the volume mounts for the init container that aggregates the config files
    cms_edxapp_init_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name=source_name,
            mount_path=f"/openedx/config-sources/{source_name}",
            read_only=True,
        )
        for source_name in cms_edxapp_config_sources
    ]
    cms_edxapp_init_volume_mounts.append(
        kubernetes.core.v1.VolumeMountArgs(
            name="edxapp-config",
            mount_path="/openedx/config",
        )
    )

    cms_labels = k8s_global_labels | {"ol.mit.edu/component": "edxapp-cms"}
    cms_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-cms-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=cms_deployment_name,
            namespace=namespace,
            labels=cms_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(match_labels=cms_labels),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=cms_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=cms_edxapp_volumes,
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image="busybox:1.35",
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/cms.env.yml"
                            ],
                            volume_mounts=cms_edxapp_init_volume_mounts,
                        )
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="busybox",  # Placeholder for the actual CMS container
                            image="busybox:1.35",
                            command=["/bin/sh", "-c", "sleep infinity"],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="edxapp-config",
                                    mount_path="/openedx/config",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name=uwsgi_ini_config_name,
                                    mount_path="/openedx/edx-platform/uwsgi.ini",
                                    sub_path="uwsgi.ini",
                                ),
                            ],
                        )
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=list(cms_edxapp_config_sources.values())
        ),
    )

    ############################################
    # lms deployment resources
    ############################################
    lms_edxapp_config_sources = {
        db_creds_secret_name: db_creds_secret,
        db_connections_secret_name: db_connections_secret,
        mongo_db_creds_secret_name: mongo_db_creds_secret,
        mongo_db_forum_secret_name: mongo_db_forum_secret,
        general_secrets_name: general_secrets_secret,
        xqueue_secret_name: xqueue_secret_secret,
        forum_secret_name: forum_secret_secret,
        general_config_name: general_config_map,
        interpolated_config_name: interpolated_config_map,
        # Just LMS specific resources below this line
        lms_oauth_secret_name: lms_oauth_secret,
        lms_interpolated_config_name: lms_interpolated_config,
    }
    lms_edxapp_secret_names = [
        db_creds_secret_name,
        db_connections_secret_name,
        mongo_db_creds_secret_name,
        mongo_db_forum_secret_name,
        general_secrets_name,
        xqueue_secret_name,
        forum_secret_name,
        # Just LMS specific resources below this line
        lms_oauth_secret_name,
    ]
    lms_edxapp_configmap_names = [
        general_config_name,
        interpolated_config_name,
        # Just LMS specific resources below this line
        lms_interpolated_config_name,
    ]

    # Define the volumes that will be mounted into the edxapp containers
    lms_edxapp_volumes = [
        kubernetes.core.v1.VolumeArgs(
            name=secret_name,
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(secret_name=secret_name),
        )
        for secret_name in lms_edxapp_secret_names
    ]
    lms_edxapp_volumes.extend(
        [
            kubernetes.core.v1.VolumeArgs(
                name=configmap_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmap_name
                ),
            )
            for configmap_name in lms_edxapp_configmap_names
        ]
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="edxapp-config",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name=uwsgi_ini_config_name,
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=uwsgi_ini_config_name
            ),
        )
    )

    # Define the volume mounts for the init container that aggregates the config files
    lms_edxapp_init_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name=source_name,
            mount_path=f"/openedx/config-sources/{source_name}",
            read_only=True,
        )
        for source_name in lms_edxapp_config_sources
    ]
    lms_edxapp_init_volume_mounts.append(
        kubernetes.core.v1.VolumeMountArgs(
            name="edxapp-config",
            mount_path="/openedx/config",
        )
    )

    lms_labels = k8s_global_labels | {"ol.mit.edu/component": "edxapp-lms"}
    lms_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-lms-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=lms_deployment_name,
            namespace=namespace,
            labels=lms_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(match_labels=lms_labels),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=lms_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=lms_edxapp_volumes,
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image="busybox:1.35",
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml"
                            ],
                            volume_mounts=lms_edxapp_init_volume_mounts,
                        )
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="busybox",  # Placeholder for the actual LMS container
                            image="busybox:1.35",
                            command=["/bin/sh", "-c", "sleep infinity"],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="edxapp-config",
                                    mount_path="/openedx/config",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name=uwsgi_ini_config_name,
                                    mount_path="/openedx/edx-platform/uwsgi.ini",
                                    sub_path="uwsgi.ini",
                                ),
                            ],
                        )
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=list(lms_edxapp_config_sources.values())
        ),
    )

    return {
        "edxapp_k8s_app_security_group_id": edxapp_k8s_app_security_group.id,
    }
