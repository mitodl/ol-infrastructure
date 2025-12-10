# ruff: noqa: E501, S105, PLR0913
import textwrap
from dataclasses import dataclass

from pulumi import Config, Output, ResourceOptions, StackReference

from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.aws.database import OLAmazonDB
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def _build_general_secrets_template(
    stack_info: StackInfo,
    redis_hostname: str,
    lms_domain: str,
    proctortrack_url: str | None,
) -> str:
    """Build the general secrets YAML template with conditional sections.

    This function generates the 10-general-secrets.yaml template, extracting
    complex conditionals out of nested f-strings for better readability.
    """
    # Base template with common configuration
    template_parts = [
        f"""
        CELERY_BROKER_PASSWORD: {{{{ get .Secrets "redis_auth_token" }}}}
        FERNET_KEYS: {{{{ get .Secrets "fernet_keys" }}}}
        redis_cache_config: &redis_cache_config
          BACKEND: django_redis.cache.RedisCache
          LOCATION: rediss://default@{redis_hostname}:6379/0
          KEY_FUNCTION: common.djangoapps.util.memcache.safe_key
          OPTIONS:
            CLIENT_CLASS: django_redis.client.DefaultClient
            PASSWORD: {{{{ get .Secrets "redis_auth_token" }}}}
        SECRET_KEY: {{{{ get .Secrets "django_secret_key" }}}}
        JWT_AUTH:  # NEEDS ATTENTION
          JWT_ALGORITHM: HS256
          JWT_AUDIENCE: {stack_info.env_prefix}
          JWT_AUTH_COOKIE: {stack_info.env_prefix}-{stack_info.env_suffix}-edx-jwt-cookie
          JWT_AUTH_COOKIE_HEADER_PAYLOAD: {stack_info.env_prefix}-{stack_info.env_suffix}-edx-jwt-cookie-header-payload
          JWT_AUTH_COOKIE_SIGNATURE: {stack_info.env_prefix}-{stack_info.env_suffix}-edx-jwt-cookie-signature
          JWT_ISSUER: 'https://{lms_domain}/oauth2'
          JWT_LOGIN_CLIENT_ID: login-service-client-id
          JWT_LOGIN_SERVICE_USERNAME: login_service_user
          JWT_PRIVATE_SIGNING_JWK: '{{{{ get .Secrets "private_signing_jwk" }}}}'
          JWT_PUBLIC_SIGNING_JWK_SET: '{{{{ get .Secrets "public_signing_jwk" }}}}'
          JWT_SECRET_KEY: {{{{ get .Secrets "django_secret_key" }}}}
          JWT_SIGNING_ALGORITHM: RS512
          JWT_ISSUERS:
            - ISSUER: https://{lms_domain}/oauth2
              AUDIENCE: {stack_info.env_prefix}
              SECRET_KEY: {{{{ get .Secrets "django_secret_key" }}}}
        OPENAI_SECRET_KEY: {{{{ get .Secrets "openai_api_key" }}}}
        OPENAI_API_KEY: {{{{ get .Secrets "openai_api_key" }}}}
        RETIRED_USER_SALTS: {{{{ get .Secrets "user_retirement_salts" }}}}
        SENTRY_DSN: {{{{ get .Secrets "sentry_dsn" }}}}
        SYSADMIN_GITHUB_WEBHOOK_KEY: {{{{ get .Secrets "sysadmin_git_webhook_secret" }}}}"""
    ]

    if stack_info.env_prefix == "mitx":
        template_parts.append(
            """
        CANVAS_ACCESS_TOKEN: {{ get .Secrets "canvas_access_token" }}"""
        )

    if stack_info.env_prefix == "mitxonline":
        template_parts.append(
            """
        GITHUB_ACCESS_TOKEN: {{ get .Secrets "github_access_token" }}
        DEEPL_API_KEY: {{ get .Secrets "deepl_api_key" }}"""
        )

    # Conditional: xpro and mitx specific email configuration
    if stack_info.env_prefix in ("xpro", "mitx"):
        template_parts.append(
            """
        EMAIL_HOST_USER: {{ get .Secrets "email_username" }}
        EMAIL_HOST_PASSWORD: {{ get .Secrets "email_password" }}"""
        )

    # Proctoring backend configuration
    default_backend = "null" if stack_info.env_prefix == "xpro" else "proctortrack"
    template_parts.append(
        f"""
        PROCTORING_BACKENDS:
          DEFAULT: '{default_backend}'"""
    )

    # Conditional: proctortrack configuration
    if proctortrack_url:
        template_parts.append(
            f"""
          'proctortrack':
            'client_id': '{{{{ get .Secrets "proctortrack_client_id" }}}}'
            'client_secret': '{{{{ get .Secrets "proctortrack_client_secret" }}}}'
            'base_url': '{proctortrack_url}'"""
        )

    template_parts.append(
        """
          'null': {}"""
    )

    # Conditional: proctortrack user obfuscation key
    if proctortrack_url:
        template_parts.append(
            """
        PROCTORING_USER_OBFUSCATION_KEY: {{ get .Secrets "proctortrack_user_obfuscation_key" }}"""
        )

    return textwrap.dedent("".join(template_parts))


@dataclass
class EdxappSecrets:
    db_creds: Output
    db_connections: Output
    mongo_db_creds: OLVaultK8SSecret
    mongo_db_forum: OLVaultK8SSecret
    general: Output
    xqueue: OLVaultK8SSecret | None
    forum: OLVaultK8SSecret
    learn_ai_canvas_syllabus_token: OLVaultK8SSecret
    cms_oauth: OLVaultK8SSecret
    lms_oauth: OLVaultK8SSecret

    db_creds_secret_name: str
    db_connections_secret_name: str
    mongo_db_creds_secret_name: str
    mongo_db_forum_secret_name: str
    general_secrets_name: str
    xqueue_secret_name: str | None
    forum_secret_name: str
    learn_ai_canvas_syllabus_token_secret_name: str
    cms_oauth_secret_name: str
    lms_oauth_secret_name: str


def create_k8s_secrets(
    edxapp_cache: OLAmazonCache,
    edxapp_config: Config,
    edxapp_db: OLAmazonDB,
    k8s_global_labels: dict[str, str],
    mongodb_atlas_stack: StackReference,
    namespace: str,
    stack_info: StackInfo,
    vault_k8s_resources: OLVaultK8SResources,
) -> EdxappSecrets:
    """Create all the k8s secrets needed for edxapp."""
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
            opts=ResourceOptions(
                delete_before_replace=True, depends_on=[vault_k8s_resources]
            ),
        ),
    )

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
                          ssl_mode: REQUIRED
                          ssl:
                            cipher: TLSv1.2
                          charset: utf8mb4
                        <<: *mysql_creds
                      read_replica:
                        CONN_MAX_AGE: 0
                        NAME: edxapp
                        OPTIONS:
                          ssl_mode: REQUIRED
                          ssl:
                            cipher: TLSv1.2
                          charset: utf8mb4
                        <<: *mysql_creds
                      student_module_history:
                        CONN_MAX_AGE: 0
                        ENGINE: django.db.backends.mysql
                        HOST: {db["address"]}
                        PORT: {db["port"]}
                        NAME: edxapp_csmh
                        OPTIONS:
                          ssl_mode: REQUIRED
                          ssl:
                            cipher: TLSv1.2
                          charset: utf8mb4
                        USER: {{{{ get .Secrets "username" }}}}
                        PASSWORD: {{{{ get .Secrets "password" }}}}
                    """)
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=ResourceOptions(
                delete_before_replace=True, depends_on=[vault_k8s_resources]
            ),
        ),
    )

    mongo_db_creds_secret_name = (
        "02-mongodb-credentials-yaml"  # pragma: allowlist secret
    )
    mongo_db_creds_secret = Output.all(
        replica_set=mongodb_atlas_stack.require_output("atlas_cluster")["replica_set"],
        host_string=mongodb_atlas_stack.require_output("atlas_cluster")[
            "public_host_string"
        ],
    ).apply(
        lambda mongodb: OLVaultK8SSecret(
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
                    "02-mongo-db-credentials.yaml": textwrap.dedent(f"""
                    mongodb_settings: &mongo_params
                      authsource: admin
                      host: {mongodb["host_string"]}
                      port: 27017
                      db: edxapp
                      replicaSet: {mongodb["replica_set"]}
                      user: {{{{ get .Secrets "username" }}}}
                      password: {{{{ get .Secrets "password" }}}}
                      ssl: true
                """),
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=ResourceOptions(
                delete_before_replace=True, depends_on=[vault_k8s_resources]
            ),
        )
    )

    mongo_db_forum_secret_name = (
        "03-mongodb-forum-credentials-yaml"  # pragma: allowlist secret
    )
    mongo_db_forum_secret = Output.all(
        replica_set=mongodb_atlas_stack.require_output("atlas_cluster")["replica_set"],
        host_string=mongodb_atlas_stack.require_output("atlas_cluster")[
            "public_host_string"
        ],
    ).apply(
        lambda mongodb: OLVaultK8SSecret(
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
                    "03-mongo-db-forum-credentials.yaml": textwrap.dedent(f"""
                        FORUM_MONGODB_CLIENT_PARAMETERS:
                          authSource: admin
                          host: {mongodb["host_string"]}
                          port: 27017
                          replicaSet: {mongodb["replica_set"]}
                          username: {{{{ get .Secrets "username" }}}}
                          password: {{{{ get .Secrets "password" }}}}
                          ssl: true
                    """),
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=ResourceOptions(
                delete_before_replace=True, depends_on=[vault_k8s_resources]
            ),
        )
    )

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
                    "10-general-secrets.yaml": _build_general_secrets_template(
                        stack_info=stack_info,
                        redis_hostname=redis_cache["redis_hostname"],
                        lms_domain=edxapp_config.require_object("domains")["lms"],
                        proctortrack_url=edxapp_config.get("proctortrack_url"),
                    ),
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=ResourceOptions(
                delete_before_replace=True, depends_on=[vault_k8s_resources]
            ),
        ),
    )

    if edxapp_config.get_bool("enable_xqueue"):
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
                              url: http://xqueue:8040 # Assumes Xqueue is running in K8S
                        """),
                },
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=ResourceOptions(
                delete_before_replace=True, depends_on=[vault_k8s_resources]
            ),
        )
    else:
        xqueue_secret_name = None
        xqueue_secret_secret = None

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
        opts=ResourceOptions(
            delete_before_replace=True, depends_on=[vault_k8s_resources]
        ),
    )

    learn_ai_canvas_syllabus_token_secret_name = (
        "13-canvas-syllabus-token-yaml"  # pragma: allowlist secret
    )
    learn_ai_canvas_syllabus_token_secret_secret = OLVaultK8SSecret(
        f"ol-{stack_info.env_prefix}-edxapp-learn-ai-canvas-syllabus-token-secret-{stack_info.env_suffix}",
        OLVaultK8SStaticSecretConfig(
            name=learn_ai_canvas_syllabus_token_secret_name,
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=learn_ai_canvas_syllabus_token_secret_name,
            labels=k8s_global_labels,
            mount="secret-global",
            mount_type="kv-v2",
            path="learn_ai",
            templates={
                "13-canvas-syllabus-token-secrets.yaml": textwrap.dedent("""
                       MIT_LEARN_AI_XBLOCK_CHAT_API_TOKEN: {{ get .Secrets "canvas_syllabus_token" }}
                    """),
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True, depends_on=[vault_k8s_resources]
        ),
    )

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
        opts=ResourceOptions(
            delete_before_replace=True, depends_on=[vault_k8s_resources]
        ),
    )

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
                "80-lms-oauth-credentials.yaml": textwrap.dedent(f"""
                    SOCIAL_AUTH_OAUTH_SECRETS:
                        ol-oauth2: {{{{ get .Secrets "{stack_info.env_prefix}_oauth_secret" }}}}
                """),
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True, depends_on=[vault_k8s_resources]
        ),
    )

    return EdxappSecrets(
        db_creds=db_creds_secret,
        db_connections=db_connections_secret,
        mongo_db_creds=mongo_db_creds_secret,
        mongo_db_forum=mongo_db_forum_secret,
        general=general_secrets_secret,
        xqueue=xqueue_secret_secret,
        forum=forum_secret_secret,
        learn_ai_canvas_syllabus_token=learn_ai_canvas_syllabus_token_secret_secret,
        cms_oauth=cms_oauth_secret,
        lms_oauth=lms_oauth_secret,
        db_creds_secret_name=db_creds_secret_name,
        db_connections_secret_name=db_connections_secret_name,
        mongo_db_creds_secret_name=mongo_db_creds_secret_name,
        mongo_db_forum_secret_name=mongo_db_forum_secret_name,
        general_secrets_name=general_secrets_name,
        xqueue_secret_name=xqueue_secret_name,
        forum_secret_name=forum_secret_name,
        learn_ai_canvas_syllabus_token_secret_name=learn_ai_canvas_syllabus_token_secret_name,
        cms_oauth_secret_name=cms_oauth_secret_name,
        lms_oauth_secret_name=lms_oauth_secret_name,
    )
