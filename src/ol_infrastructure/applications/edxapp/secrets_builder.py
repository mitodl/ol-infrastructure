# ruff: noqa: E501, S105
# mypy: ignore-errors
"""Secret configuration builder for edxapp Kubernetes secrets.

This module builds secret dictionaries and YAML templates for edxapp,
replacing the repetitive f-string concatenation approach with cleaner,
testable dictionary-based configuration.
"""

import copy
from typing import Any

import yaml

from ol_infrastructure.lib.pulumi_helper import StackInfo


def build_base_general_secrets_dict(
    stack_info: StackInfo,
    redis_hostname: str,
    lms_domain: str,
    proctortrack_url: str | None = None,
) -> dict[str, Any]:
    """Build general secrets as a dictionary (replaces f-string template).

    This function generates the core secrets configuration, making it
    testable and easier to understand deployment-specific settings.

    Args:
        stack_info: Stack information with env_prefix and env_suffix
        redis_hostname: Redis cache hostname
        lms_domain: LMS domain for JWT configuration
        proctortrack_url: Optional Proctortrack base URL

    Returns:
        Dictionary of secrets configuration
    """
    # Base secrets shared across all deployments
    base_secrets: dict[str, Any] = {
        "CELERY_BROKER_PASSWORD": '{{ get .Secrets "redis_auth_token" }}',
        "FERNET_KEYS": '{{ get .Secrets "fernet_keys" }}',
        "redis_cache_config": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": f"rediss://default@{redis_hostname}:6379/0",
            "KEY_FUNCTION": "common.djangoapps.util.memcache.safe_key",
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "PASSWORD": '{{ get .Secrets "redis_auth_token" }}',
            },
        },
        "SECRET_KEY": '{{ get .Secrets "django_secret_key" }}',
        "JWT_AUTH": {
            "JWT_ALGORITHM": "HS256",
            "JWT_AUDIENCE": stack_info.env_prefix,
            "JWT_AUTH_COOKIE": f"{stack_info.env_prefix}-{stack_info.env_suffix}-edx-jwt-cookie",
            "JWT_AUTH_COOKIE_HEADER_PAYLOAD": f"{stack_info.env_prefix}-{stack_info.env_suffix}-edx-jwt-cookie-header-payload",
            "JWT_AUTH_COOKIE_SIGNATURE": f"{stack_info.env_prefix}-{stack_info.env_suffix}-edx-jwt-cookie-signature",
            "JWT_ISSUER": f"https://{lms_domain}/oauth2",
            "JWT_LOGIN_CLIENT_ID": "login-service-client-id",
            "JWT_LOGIN_SERVICE_USERNAME": "login_service_user",
            "JWT_PRIVATE_SIGNING_JWK": '{{ get .Secrets "private_signing_jwk" }}',
            "JWT_PUBLIC_SIGNING_JWK_SET": '{{ get .Secrets "public_signing_jwk" }}',
            "JWT_SECRET_KEY": '{{ get .Secrets "django_secret_key" }}',
            "JWT_SIGNING_ALGORITHM": "RS512",
            "JWT_ISSUERS": [
                {
                    "ISSUER": f"https://{lms_domain}/oauth2",
                    "AUDIENCE": stack_info.env_prefix,
                    "SECRET_KEY": '{{ get .Secrets "django_secret_key" }}',
                }
            ],
        },
        "OPENAI_SECRET_KEY": '{{ get .Secrets "openai_api_key" }}',
        "OPENAI_API_KEY": '{{ get .Secrets "openai_api_key" }}',
        "RETIRED_USER_SALTS": '{{ get .Secrets "user_retirement_salts" }}',
        "SENTRY_DSN": '{{ get .Secrets "sentry_dsn" }}',
        "SYSADMIN_GITHUB_WEBHOOK_KEY": '{{ get .Secrets "sysadmin_git_webhook_secret" }}',
    }

    # Apply deployment-specific overrides
    return _apply_deployment_secret_overrides(
        stack_info.env_prefix, base_secrets, proctortrack_url
    )


def _apply_deployment_secret_overrides(
    env_prefix: str,
    base_secrets: dict[str, Any],
    proctortrack_url: str | None = None,
) -> dict[str, Any]:
    """Apply per-deployment secret overrides to base configuration.

    Args:
        env_prefix: Environment prefix (mitx, xpro, mitxonline, mitx-staging)
        base_secrets: Base secrets dictionary to override
        proctortrack_url: Optional Proctortrack URL for conditional settings

    Returns:
        Merged secrets dictionary with deployment-specific settings
    """
    # Copy to avoid mutating input
    secrets = dict(base_secrets)

    # mitx-specific configuration
    if env_prefix == "mitx":
        secrets["CANVAS_ACCESS_TOKEN"] = '{{ get .Secrets "canvas_access_token" }}'

    # mitxonline-specific configuration
    if env_prefix == "mitxonline":
        secrets["GITHUB_ACCESS_TOKEN"] = '{{ get .Secrets "github_access_token" }}'
        secrets["DEEPL_API_KEY"] = '{{ get .Secrets "deepl_api_key" }}'

    # xpro and mitx share email configuration
    if env_prefix in ("xpro", "mitx"):
        secrets["EMAIL_HOST_USER"] = '{{ get .Secrets "email_username" }}'
        secrets["EMAIL_HOST_PASSWORD"] = '{{ get .Secrets "email_password" }}'

    # mitx and mitx-staging have SAML configuration and YouTube API key
    if env_prefix in ("mitx", "mitx-staging"):
        secrets["SOCIAL_AUTH_SAML_SP_PRIVATE_KEY"] = (
            '{{ get .Secrets "saml_private_key" }}'
        )
        secrets["SOCIAL_AUTH_SAML_SP_PUBLIC_CERT"] = (
            '{{ get .Secrets "saml_public_cert" }}'
        )
        secrets["YOUTUBE_API_KEY"] = '{{ get .Secrets "youtube_api_key" }}'

    # Proctoring backend configuration
    default_backend = "null" if env_prefix == "xpro" else "proctortrack"
    secrets["PROCTORING_BACKENDS"] = {"DEFAULT": default_backend}

    if proctortrack_url:
        secrets["PROCTORING_BACKENDS"]["proctortrack"] = {
            "client_id": '{{ get .Secrets "proctortrack_client_id" }}',
            "client_secret": '{{ get .Secrets "proctortrack_client_secret" }}',
            "base_url": proctortrack_url,
        }
        secrets["PROCTORING_USER_OBFUSCATION_KEY"] = (
            '{{ get .Secrets "proctortrack_user_obfuscation_key" }}'
        )

    # Always include null backend
    secrets["PROCTORING_BACKENDS"]["null"] = {}

    return secrets


def secrets_dict_to_yaml_template(secrets: dict[str, Any]) -> str:
    """Convert secrets dictionary to Vault YAML template string.

    Args:
        secrets: Dictionary of secrets configuration

    Returns:
        YAML string suitable for Vault templating
    """

    # Extract FERNET_KEYS to render it unquoted (as raw Vault template)
    fernet_keys = secrets.pop("FERNET_KEYS", None)
    retired_user_salts = secrets.pop("RETIRED_USER_SALTS", None)

    # Custom representer for strings (control quoting behavior)
    def _str_representer(dumper: Any, data: str) -> Any:
        # Use default YAML string representation (which quotes as needed)
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    # Create custom dumper class
    class CustomDumper(yaml.Dumper):
        pass

    # Register custom representer
    CustomDumper.add_representer(str, _str_representer)

    # Override ignore_aliases to disable YAML anchors/aliases
    CustomDumper.ignore_aliases = lambda self, data: True  # type: ignore[method-assign]  # noqa: ARG005

    # Dump to YAML
    yaml_output = yaml.dump(
        secrets, Dumper=CustomDumper, default_flow_style=False, sort_keys=False
    )

    # Prepend FERNET_KEYS as unquoted Vault template
    if fernet_keys:
        yaml_output = (
            f"FERNET_KEYS: {fernet_keys}\nRETIRED_USER_SALTS: {retired_user_salts}\n"
            + yaml_output
        )

    # Restore FERNET_KEYS to dict in case it's used again
    if fernet_keys:
        secrets["FERNET_KEYS"] = fernet_keys

    return yaml_output


def get_database_credentials_template(db_address: str, db_port: int) -> tuple[str, str]:
    """Generate database credentials template for vault.

    Args:
        db_address: Database hostname/address
        db_port: Database port

    Returns:
        Tuple of (template_string, template_name)
    """
    config = {
        "mysql_creds": {
            "ENGINE": "django.db.backends.mysql",
            "HOST": db_address,
            "PORT": db_port,
            "USER": '{{ get .Secrets "username" }}',
            "PASSWORD": '{{ get .Secrets "password" }}',
        }
    }

    yaml_template = secrets_dict_to_yaml_template(config)
    return yaml_template, "00-database-credentials.yaml"


def get_database_connections_template(db_address: str, db_port: int) -> tuple[str, str]:
    """Generate database connections template for vault.

    Args:
        db_address: Database hostname/address
        db_port: Database port

    Returns:
        Tuple of (template_string, template_name)
    """
    # Base MySQL credentials
    mysql_creds = {
        "ENGINE": "django.db.backends.mysql",
        "HOST": db_address,
        "PORT": db_port,
        "USER": '{{ get .Secrets "username" }}',
        "PASSWORD": '{{ get .Secrets "password" }}',
    }

    # Common database options
    db_options = {
        "ssl_mode": "REQUIRED",
        "ssl": {"cipher": "TLSv1.2"},
        "charset": "utf8mb4",
    }

    config = {
        "DATABASES": {
            "default": {
                "ATOMIC_REQUESTS": True,
                "CONN_MAX_AGE": 0,
                "NAME": "edxapp",
                "OPTIONS": copy.deepcopy(db_options),
                **copy.deepcopy(mysql_creds),
            },
            "read_replica": {
                "CONN_MAX_AGE": 0,
                "NAME": "edxapp",
                "OPTIONS": copy.deepcopy(db_options),
                **copy.deepcopy(mysql_creds),
            },
            "student_module_history": {
                "CONN_MAX_AGE": 0,
                "ENGINE": "django.db.backends.mysql",
                "HOST": db_address,
                "PORT": db_port,
                "NAME": "edxapp_csmh",
                "OPTIONS": copy.deepcopy(db_options),
                "USER": '{{ get .Secrets "username" }}',
                "PASSWORD": '{{ get .Secrets "password" }}',
            },
        }
    }

    yaml_template = secrets_dict_to_yaml_template(config)
    return yaml_template, "01-database-connections.yaml"


def get_mongodb_credentials_template(
    replica_set: str, host_string: str
) -> tuple[str, str]:
    """Generate MongoDB credentials template for vault.

    Includes DOC_STORE_CONFIG, CONTENTSTORE, and MODULESTORE configs
    with duplicated MongoDB connection values.

    Args:
        replica_set: MongoDB replica set name
        host_string: MongoDB host string

    Returns:
        Tuple of (template_string, template_name)
    """
    # Base MongoDB connection parameters
    mongo_params = {
        "authsource": "admin",
        "host": host_string,
        "port": 27017,
        "db": "edxapp",
        "replicaSet": replica_set,
        "user": '{{ get .Secrets "username" }}',
        "password": '{{ get .Secrets "password" }}',
        "ssl": True,
    }

    # DOC_STORE_CONFIG with MongoDB params
    doc_store_config = {
        "collection": "modulestore",
        "connectTimeoutMS": 2000,
        "socketTimeoutMS": 3000,
        **copy.deepcopy(mongo_params),
    }

    # Build complete config structure
    config = {
        "mongodb_settings": copy.deepcopy(mongo_params),
        "DOC_STORE_CONFIG": copy.deepcopy(doc_store_config),
        "CONTENTSTORE": {
            "ADDITIONAL_OPTIONS": {},
            "DOC_STORE_CONFIG": copy.deepcopy(doc_store_config),
            "ENGINE": "xmodule.contentstore.mongo.MongoContentStore",
            "OPTIONS": {
                "auth_source": "",
                **copy.deepcopy(mongo_params),
            },
        },
        "MODULESTORE": {
            "default": {
                "ENGINE": "xmodule.modulestore.mixed.MixedModuleStore",
                "OPTIONS": {
                    "mappings": {},
                    "stores": [
                        {
                            "ENGINE": "xmodule.modulestore.split_mongo.split_draft.DraftVersioningModuleStore",
                            "NAME": "split",
                            "DOC_STORE_CONFIG": copy.deepcopy(doc_store_config),
                            "OPTIONS": {
                                "default_class": "xmodule.hidden_block.HiddenBlock",
                                "fs_root": "/openedx/data/var/edxapp/data",
                                "render_template": "common.djangoapps.edxmako.shortcuts.render_to_string",
                            },
                        },
                        {
                            "ENGINE": "xmodule.modulestore.mongo.DraftMongoModuleStore",
                            "NAME": "draft",
                            "DOC_STORE_CONFIG": copy.deepcopy(doc_store_config),
                            "OPTIONS": {
                                "default_class": "xmodule.hidden_block.HiddenBlock",
                                "fs_root": "/openedx/data/var/edxapp/data",
                                "render_template": "common.djangoapps.edxmako.shortcuts.render_to_string",
                            },
                        },
                    ],
                },
            },
        },
    }

    # Convert to YAML using the helper function
    yaml_template = secrets_dict_to_yaml_template(config)

    return yaml_template, "02-mongo-db-credentials.yaml"


def get_mongodb_forum_template(replica_set: str, host_string: str) -> tuple[str, str]:
    """Generate MongoDB forum template for vault.

    Args:
        replica_set: MongoDB replica set name
        host_string: MongoDB host string

    Returns:
        Tuple of (template_string, template_name)
    """
    config = {
        "FORUM_MONGODB_CLIENT_PARAMETERS": {
            "authSource": "admin",
            "host": host_string,
            "port": 27017,
            "replicaSet": replica_set,
            "username": '{{ get .Secrets "username" }}',
            "password": '{{ get .Secrets "password" }}',
            "ssl": True,
        }
    }

    yaml_template = secrets_dict_to_yaml_template(config)
    return yaml_template, "03-mongo-db-forum-credentials.yaml"


def get_general_secrets_yaml(
    stack_info: StackInfo,
    redis_hostname: str,
    lms_domain: str,
    proctortrack_url: str | None = None,
) -> str:
    """Build complete general secrets YAML template.

    This replaces the old _build_general_secrets_template() function.
    Includes CACHES, CONTENTSTORE, and DOC_STORE_CONFIG with duplicated
    values instead of YAML anchors/aliases.

    Args:
        stack_info: Stack information
        redis_hostname: Redis cache hostname
        lms_domain: LMS domain for JWT config
        proctortrack_url: Optional Proctortrack URL

    Returns:
        YAML string ready for Vault templating
    """
    # Build base secrets
    secrets = build_base_general_secrets_dict(
        stack_info, redis_hostname, lms_domain, proctortrack_url
    )

    # Add CACHES configuration (duplicates redis_cache_config for each cache)
    redis_cache_base = secrets.pop("redis_cache_config")
    secrets["CACHES"] = {
        "celery": {
            **copy.deepcopy(redis_cache_base),
            "KEY_PREFIX": "celery",
            "TIMEOUT": "7200",
        },
        "configuration": {
            **copy.deepcopy(redis_cache_base),
            "KEY_PREFIX": "configuration",
        },
        "course_structure_cache": {
            **copy.deepcopy(redis_cache_base),
            "KEY_PREFIX": "course_structure",
            "TIMEOUT": "7200",
        },
        "default": {
            **copy.deepcopy(redis_cache_base),
            "KEY_PREFIX": "default",
            "VERSION": "1",
        },
        "general": {**copy.deepcopy(redis_cache_base), "KEY_PREFIX": "general"},
        "mongo_metadata_inheritance": {
            **copy.deepcopy(redis_cache_base),
            "KEY_PREFIX": "mongo_metadata_inheritance",
            "TIMEOUT": 300,
        },
        "staticfiles": {**copy.deepcopy(redis_cache_base), "KEY_PREFIX": "staticfiles"},
    }

    # Apply deployment overrides
    secrets = _apply_deployment_secret_overrides(stack_info.env_prefix, secrets)

    return secrets_dict_to_yaml_template(secrets)
