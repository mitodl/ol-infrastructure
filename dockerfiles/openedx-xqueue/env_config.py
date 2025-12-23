# ruff: noqa: INP001
"""
Django settings module for xqueue that loads configuration from
environment variables. This replaces the yaml_config.py settings module
when running in Kubernetes.

This module can be used by setting DJANGO_SETTINGS_MODULE=xqueue.env_config
in the Docker image or deployment configuration.
"""

import json
import os
from typing import Any

from django.core.exceptions import ImproperlyConfigured

# Import all defaults from xqueue's settings
from xqueue.settings import *  # noqa: F403

# Explicitly declare security settings
DEBUG = False
TEMPLATE_DEBUG = False

# Required environment variables
REQUIRED_ENV_VARS = [
    "DB_HOST",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
]

# Validate required environment variables
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
if missing_vars:
    missing_vars_str = ", ".join(missing_vars)
    msg = f"Missing required environment variables: {missing_vars_str}"
    raise ImproperlyConfigured(msg)

# Database Configuration
DATABASES = {
    "default": {
        "ATOMIC_REQUESTS": True,
        "CONN_MAX_AGE": 0,
        "ENGINE": os.environ.get("DB_ENGINE", "django.db.backends.mysql"),
        "HOST": os.environ["DB_HOST"],
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        "PORT": int(os.environ.get("DB_PORT", "3306")),
        "OPTIONS": {
            "ssl_mode": "REQUIRED",
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
    if value:
        DATABASES["default"][override] = value

# XQueue User Credentials
# These credentials are used to authenticate internal services like edxapp
# and xqwatcher that submit jobs to the queue
USERS = {}
if os.environ.get("XQUEUE_EDXAPP_PASSWORD"):
    USERS["edxapp"] = os.environ["XQUEUE_EDXAPP_PASSWORD"]
if os.environ.get("XQUEUE_XQWATCHER_PASSWORD"):
    USERS["xqwatcher"] = os.environ["XQUEUE_XQWATCHER_PASSWORD"]

# AWS S3 Configuration for submission uploads
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", "")
UPLOAD_PATH_PREFIX = os.environ.get("UPLOAD_PATH_PREFIX", "xqueue")

# Queue Processing Configuration
CONSUMER_DELAY = int(os.environ.get("CONSUMER_DELAY", "10"))
SUBMISSION_PROCESSING_DELAY = int(os.environ.get("SUBMISSION_PROCESSING_DELAY", "1"))

# Security Configuration
CSRF_COOKIE_SECURE = os.environ.get("CSRF_COOKIE_SECURE", "false").lower() == "true"
SESSION_COOKIE_SECURE = (
    os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
)

# Logging Configuration
LOCAL_LOGLEVEL = os.environ.get("LOCAL_LOGLEVEL", "INFO")
LOGGING_ENV = os.environ.get("LOGGING_ENV", "dev")
LOG_DIR = os.environ.get("LOG_DIR", "/edx/var/logs/xqueue")
SYSLOG_SERVER = os.environ.get("SYSLOG_SERVER", "localhost")
NEWRELIC_LICENSE_KEY = os.environ.get("NEWRELIC_LICENSE_KEY", "not-a-valid-key")

# XQueue Configuration
# Map of queue names to queue handlers
# Each queue can have its own handler URL or be null for default handling
XQUEUES: dict[str, Any] = {}
if os.environ.get("XQUEUES"):
    try:
        XQUEUES = json.loads(os.environ["XQUEUES"])
    except json.JSONDecodeError as err:
        msg = "XQUEUES environment variable must be valid JSON"
        raise ImproperlyConfigured(msg) from err
else:
    # Default queues for MIT deployments
    XQUEUES = {
        "Watcher-MITx-6.0001r": None,
        "Watcher-MITx-6.00x": None,
        "mitx-686xgrader": None,
        "mitx-6S082grader": None,
        "mitx-940grader": None,
    }

# Database host for services (e.g., edxapp-db.service.consul)
# Used for internal service discovery
DB_HOST_SERVICE = os.environ.get("DB_HOST_SERVICE", os.environ["DB_HOST"])

# Additional optional configuration from environment
allowed_hosts_str = os.environ.get("ALLOWED_HOSTS", "*")
if isinstance(allowed_hosts_str, str):
    ALLOWED_HOSTS = allowed_hosts_str.split(",")
else:
    ALLOWED_HOSTS = allowed_hosts_str

CSRF_TRUSTED_ORIGINS = []
if os.environ.get("CSRF_TRUSTED_ORIGINS"):
    origins_str = os.environ["CSRF_TRUSTED_ORIGINS"]
    CSRF_TRUSTED_ORIGINS = (
        origins_str.split(",") if isinstance(origins_str, str) else origins_str
    )
