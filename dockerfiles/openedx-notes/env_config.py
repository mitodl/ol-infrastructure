# ruff: noqa: INP001
"""
Django settings module for edx-notes-api that loads configuration from
environment variables. This replaces the yaml_config.py settings module
when running in Kubernetes.
"""

import json
import os

from django.core.exceptions import ImproperlyConfigured
from notesserver.settings.common import *  # noqa: F403

# Explicitly declare security settings
DEBUG = False
TEMPLATE_DEBUG = False
DISABLE_TOKEN_CHECK = False

# Required environment variables
REQUIRED_ENV_VARS = [
    "DB_HOST",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DJANGO_SECRET_KEY",
    "OAUTH_CLIENT_ID",
    "OAUTH_CLIENT_SECRET",
    "ELASTICSEARCH_DSL_HOST",
]

# Validate required environment variables
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
if missing_vars:
    missing_vars_str = ", ".join(missing_vars)
    msg = f"Missing required environment variables: {missing_vars_str}"
    raise ImproperlyConfigured(msg)

# Django Secret Key
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

# OAuth Configuration
CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]

# Database Configuration
DATABASES = {
    "default": {
        "ENGINE": os.environ.get("DB_ENGINE", "django.db.backends.mysql"),
        "HOST": os.environ["DB_HOST"],
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        "PORT": int(os.environ.get("DB_PORT", "3306")),
        "OPTIONS": {
            "ssl_mode": "REQUIRE",
            "ssl": {"cipher": "TLSv1.2"},
            "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
        },
    }
}

# Support environment overrides for migrations
DB_OVERRIDES = {
    "PASSWORD": os.environ.get("DB_MIGRATION_PASS", DATABASES["default"]["PASSWORD"]),
    "ENGINE": os.environ.get("DB_MIGRATION_ENGINE", DATABASES["default"]["ENGINE"]),
    "USER": os.environ.get("DB_MIGRATION_USER", DATABASES["default"]["USER"]),
    "NAME": os.environ.get("DB_MIGRATION_NAME", DATABASES["default"]["NAME"]),
    "HOST": os.environ.get("DB_MIGRATION_HOST", DATABASES["default"]["HOST"]),
    "PORT": os.environ.get("DB_MIGRATION_PORT", DATABASES["default"]["PORT"]),
}

for override, value in DB_OVERRIDES.items():
    DATABASES["default"][override] = value

# Elasticsearch Configuration
ES_DISABLED = os.environ.get("ELASTICSEARCH_DSL_DISABLED", "false").lower() == "true"

if not ES_DISABLED:
    ELASTICSEARCH_DSL = {
        "default": {
            "hosts": os.environ["ELASTICSEARCH_DSL_HOST"],
            "port": int(os.environ.get("ELASTICSEARCH_DSL_PORT", "9200")),
            "use_ssl": os.environ.get("ELASTICSEARCH_DSL_USE_SSL", "false").lower()
            == "true",
            "verify_certs": os.environ.get(
                "ELASTICSEARCH_DSL_VERIFY_CERTS", "true"
            ).lower()
            == "true",
        }
    }
else:
    ELASTICSEARCH_DSL = {}

# Storage Configuration
STORAGES = {
    "default": {
        "BACKEND": os.environ.get(
            "DEFAULT_FILE_STORAGE", "django.core.files.storage.FileSystemStorage"
        ),
    },
    "staticfiles": {
        "BACKEND": os.environ.get(
            "STATICFILES_STORAGE",
            "django.contrib.staticfiles.storage.StaticFilesStorage",
        ),
    },
}

# JWT Configuration
JWT_AUTH = {
    "JWT_AUTH_HEADER_PREFIX": "JWT",
    "JWT_ISSUER": json.loads(os.environ.get("JWT_ISSUER", "[]")),
    "JWT_PUBLIC_SIGNING_JWK_SET": os.environ.get("JWT_PUBLIC_SIGNING_JWK_SET"),
    "JWT_AUTH_COOKIE_HEADER_PAYLOAD": "edx-jwt-cookie-header-payload",
    "JWT_AUTH_COOKIE_SIGNATURE": "edx-jwt-cookie-signature",
    "JWT_ALGORITHM": "HS256",
}

# Additional optional configuration from environment
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")
CSRF_TRUSTED_ORIGINS = (
    os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if os.environ.get("CSRF_TRUSTED_ORIGINS")
    else []
)

# Logging configuration
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
    },
}
