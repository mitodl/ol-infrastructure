import os  # noqa: INP001

from celery.schedules import crontab
from flask_appbuilder.security.manager import AUTH_OAUTH
from superset.utils.encrypt import SQLAlchemyUtilsAdapter
from vault.aws_auth import get_vault_client

vault_addr = os.environ.get("VAULT_ADDR", "http://localhost:8200")
vault_client = get_vault_client(vault_url=vault_addr, ec2_role="superset")

SECRET_KEY = vault_client.secrets.kv.v2.read_secret(
    path="app-config", mount_point="secret-superset"
)["data"]["data"]["secret_key"]
REDIS_TOKEN = vault_client.secrets.kv.v2.read_secret(
    path="redis", mount_point="secret-superset"
)["data"]["data"]["token"]

SUPERSET_WEBSERVER_PROTOCOL = os.environ.get("SUPERSET_WEBSERVER_PROTOCOL", "https")
SUPERSET_WEBSERVER_ADDRESS = os.environ.get("SUPERSET_WEBSERVER_ADDRESS", "0.0.0.0")  # noqa: S104
SUPERSET_WEBSERVER_PORT = os.environ.get("SUPERSET_WEBSERVER_PORT", "8088")
pg_creds = vault_client.secrets.database.generate_credentials(
    name="app", mount_point="postgres-superset"
)["data"]
SQLALCHEMY_DATABASE_URI = f"postgres://{pg_creds['username']}:{pg_creds['password']}@superset-db.service.consul:5432/superset?sslmode=require"
SQLALCHEMY_ENCRYPTED_FIELD_TYPE_ADAPTER = SQLAlchemyUtilsAdapter

ENABLE_PROXY_FIX = os.environ.get("ENABLE_PROXY_FIX", "True").lower() == "true"

# -------------------------------
# White Labeling Configurations #
# --------------------------------
APP_NAME = "MIT OL Business Intelligence"
# Specify the App icon. Useful for white-labeling
APP_ICON = "/static/assets/images/superset-logo-horiz.png"

# ----------------------------------------------------
# AUTHENTICATION CONFIG
# ----------------------------------------------------
# The authentication type
# AUTH_OID : Is for OpenID
# AUTH_DB : Is for database (username/password)
# AUTH_LDAP : Is for LDAP
# AUTH_REMOTE_USER : Is for using REMOTE_USER from web server
# AUTH_TYPE = AUTH_OID  # noqa: ERA001
oidc_creds = vault_client.secrets.kv.v1.read_secret(
    path="sso/superset", mount_point="secret-operations"
)["data"]
AUTH_TYPE = AUTH_OAUTH
OAUTH_PROVIDERS = [
    {
        "name": "OL-SSO",
        "icon": "fa-key",
        "token_key": "access_token",
        "remote_app": {
            "client_id": oidc_creds["client_id"],
            "client_kwargs": {"scope": "email profile"},
            "client_secret": oidc_creds["client_secret"],
            "request_token_url": None,
            "server_metadata_url": f"{oidc_creds['url']}/.well-known/openid-configuration",  # noqa: E501
        },
    },
]

# Testing out Keycloak role mapping to Superset
# https://superset.apache.org/docs/installation/configuring-superset#mapping-ldap-or-oauth-groups-to-superset-roles
AUTH_ROLES_MAPPING = {
    "superset_admin": ["Admin"],
}

# if we should replace ALL the user's roles each login, or only on registration
AUTH_ROLES_SYNC_AT_LOGIN = True

# Will allow user self registration, allowing to create Flask users from Authorized User
AUTH_USER_REGISTRATION = True

# The default user self registration role
AUTH_USER_REGISTRATION_ROLE = "Public"

# ---------------------------------------------------
# Feature flags
# ---------------------------------------------------
# Feature flags that are set by default go here. Their values can be
# overwritten by those specified under FEATURE_FLAGS in superset_config.py
# For example, DEFAULT_FEATURE_FLAGS = { 'FOO': True, 'BAR': False } here
# and FEATURE_FLAGS = { 'BAR': True, 'BAZ': True } in superset_config.py
# will result in combined feature flags of { 'FOO': True, 'BAR': True, 'BAZ': True }

FEATURE_FLAGS: dict[str, bool] = {
    "KV_STORE": False,
    # When this feature is enabled, nested types in Presto will be
    # expanded into extra columns and/or arrays. This is experimental,
    # and doesn't work with all nested types.
    "PRESTO_EXPAND_DATA": False,
    # Exposes API endpoint to compute thumbnails
    "THUMBNAILS": False,
    "SHARE_QUERIES_VIA_KV_STORE": False,
    "TAGGING_SYSTEM": False,
    "SQLLAB_BACKEND_PERSISTENCE": True,
    "LISTVIEWS_DEFAULT_CARD_VIEW": False,
    # When True, this escapes HTML (rather than rendering it) in Markdown components
    "ESCAPE_MARKDOWN_HTML": False,
    "DASHBOARD_CROSS_FILTERS": True,
    # Feature is under active development and breaking changes are expected
    "DASHBOARD_VIRTUALIZATION": False,
    "GLOBAL_ASYNC_QUERIES": False,
    "EMBEDDED_SUPERSET": False,
    # Enables Alerts and reports new implementation
    "ALERT_REPORTS": True,
    "DASHBOARD_RBAC": False,  # TMM 2023-11-28 - Apparently this feature doesn't properly work and is not actively supported.  # noqa: E501
    "ENABLE_ADVANCED_DATA_TYPES": False,
    # Enabling ALERTS_ATTACH_REPORTS, the system sends email and slack message
    # with screenshot and link
    # Disables ALERTS_ATTACH_REPORTS, the system DOES NOT generate screenshot
    # for report with type 'alert' and sends email and slack message with only link;
    # for report with type 'report' still send with email and slack message with
    # screenshot and link
    "ALERTS_ATTACH_REPORTS": True,
    # Allow users to export full CSV of table viz type.
    # This could cause the server to run out of memory or compute.
    "ALLOW_FULL_CSV_EXPORT": False,
    "ALLOW_ADHOC_SUBQUERY": False,
    "USE_ANALAGOUS_COLORS": False,
    # Apply RLS rules to SQL Lab queries. This requires parsing and manipulating the
    # query, and might break queries and/or allow users to bypass RLS. Use with care!
    "RLS_IN_SQLLAB": False,
    # Enable caching per impersonation key (e.g username) in a datasource where user
    # impersonation is enabled
    "CACHE_IMPERSONATION": False,
    # Enable caching per user key for Superset cache (not database cache impersonation)
    "CACHE_QUERY_BY_USER": False,
    # Enable sharing charts with embedding
    "EMBEDDABLE_CHARTS": True,
    "DRILL_TO_DETAIL": True,
    "DRILL_BY": False,
    "DATAPANEL_CLOSED_BY_DEFAULT": False,
    "HORIZONTAL_FILTER_BAR": False,
    # The feature is off by default, and currently only supported in Presto and Postgres,  # noqa: E501
    # and Bigquery.
    # It also needs to be enabled on a per-database basis, by adding the key/value pair
    # `cost_estimate_enabled: true` to the database `extra` attribute.
    "ESTIMATE_QUERY_COST": False,
    # Allow users to enable ssh tunneling when creating a DB.
    # Users must check whether the DB engine supports SSH Tunnels
    # otherwise enabling this flag won't have any effect on the DB.
    "SSH_TUNNELING": False,
    "AVOID_COLORS_COLLISION": True,
    # Set to False to only allow viewing own recent activity
    # or to disallow users from viewing other users profile page
    # Do not show user info or profile in the menu
    "MENU_HIDE_USER_INFO": False,
    # Allows users to add a ``superset://`` DB that can query across databases. This is
    # an experimental feature with potential security and performance risks, so use with
    # caution. If the feature is enabled you can also set a limit for how much data is
    # returned from each database in the ``SUPERSET_META_DB_LIMIT`` configuration value
    # in this file.
    # TMM 2023-11-28 - This seems like the same thing as Redash "query set" data source,
    # which has caused a lot of annoying bugs and bad UX.
    "ENABLE_SUPERSET_META_DB": False,
    # Set to True to replace Selenium with Playwright to execute reports and thumbnails.
    # Unlike Selenium, Playwright reports support deck.gl visualizations
    # Enabling this feature flag requires installing "playwright" pip package
    "PLAYWRIGHT_REPORTS_AND_THUMBNAILS": False,
    # Set to True to enable Jinja templating in queries in SQL Lab and Explore
    "ENABLE_TEMPLATE_PROCESSING": True,
}

# Default configurator will consume the LOG_* settings below


# Custom logger for auditing queries. This can be used to send ran queries to a
# structured immutable store for auditing purposes. The function is called for
# every query ran, in both SQL Lab and charts/dashboards.
QUERY_LOGGER = None

FILTER_STATE_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 86400,
    "CACHE_KEY_PREFIX": "superset_filter_cache",
    "CACHE_REDIS_URL": "rediss://superset-redis.service.consul:6379/0",
}

# Set this API key to enable Mapbox visualizations
MAPBOX_API_KEY = os.environ.get("MAPBOX_API_KEY", "")


class CeleryConfig:  # pylint: disable=too-few-public-methods
    broker_url = "rediss://superset-redis.service.consul:6379/1"
    imports = ("superset.sql_lab", "superset.tasks.scheduler")
    result_backend = "rediss://superset-redis.service.consul:6379/2"
    redis_username = "default"
    redis_password = REDIS_TOKEN
    worker_prefetch_multiplier = 1
    task_acks_late = True
    task_annotations = {  # noqa: RUF012
        "sql_lab.get_sql_results": {
            "rate_limit": "100/s",
        },
    }
    beat_scheduler = "redbeat.RedBeatScheduler"
    beat_schedule = {  # noqa: RUF012
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": crontab(minute="*", hour="*"),
            "options": {"expires": 3600},
        },
        "reports.prune_log": {
            "task": "reports.prune_log",
            "schedule": crontab(minute=0, hour=0),
        },
    }


CELERY_CONFIG = CeleryConfig  # pylint: disable=invalid-name

#########################
# Notification Settings #
########################
# smtp server configuration
EMAIL_NOTIFICATIONS = True  # all the emails are sent using dryrun
SMTP_HOST = "email.us-east-1.amazonaws.com"
SMTP_STARTTLS = True
SMTP_SSL = False
SMTP_PORT = 587
SMTP_MAIL_FROM = "ol-data@mit.edu"
# If True creates a default SSL context with ssl.Purpose.CLIENT_AUTH using the
# default system root CA certificates.
SMTP_SSL_SERVER_AUTH = False
ENABLE_CHUNK_ENCODING = False

SLACK_API_TOKEN = vault_client.secrets.kv.v2.read_secret(
    path="app-config", mount_point="secret-superset"
)["data"]["data"]["slack_token"]
