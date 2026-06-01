# ruff: noqa: ERA001
import logging
import os
import re
from ssl import CERT_OPTIONAL

from celery.schedules import crontab
from flask import g
from flask_appbuilder.security.manager import AUTH_OAUTH
from flask_caching.backends.rediscache import RedisCache
from sqlalchemy.engine import URL
from superset.security import SupersetSecurityManager
from superset.utils.encrypt import SQLAlchemyUtilsAdapter

# Kubernetes: secrets are provided via env by Vault Secrets Operator
SECRET_KEY = os.environ.get("SECRET_KEY")
# Backward compatibility: allow either REDIS_PASSWORD or REDIS_TOKEN
REDIS_TOKEN = os.environ.get("REDIS_PASSWORD") or os.environ.get("REDIS_TOKEN", "")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = os.environ.get("REDIS_PORT", "6379")

# Build DB URI from env (populated by Vault dynamic creds via K8s secret)
DB_USER = os.environ.get("DB_USER")
DB_PASS = os.environ.get("DB_PASS")
DB_HOST = os.environ.get("DB_HOST")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "superset")
DB_SSLMODE = os.environ.get("DB_SSLMODE", "require")
_db_uri = f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SQLALCHEMY_DATABASE_URI = f"{_db_uri}?sslmode={DB_SSLMODE}" if DB_SSLMODE else _db_uri
OIDC_URL = os.environ.get("OIDC_URL")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
OIDC_REALM_PUBLIC_KEY = os.environ.get("OIDC_REALM_PUBLIC_KEY")
LOGOUT_REDIRECT_URL = f"{OIDC_URL}/protocol/openid-connect/logout"
SLACK_API_TOKEN = os.environ.get("SLACK_API_TOKEN")

SQLALCHEMY_ENCRYPTED_FIELD_TYPE_ADAPTER = SQLAlchemyUtilsAdapter

SUPERSET_WEBSERVER_PROTOCOL = os.environ.get("SUPERSET_WEBSERVER_PROTOCOL", "https")
SUPERSET_WEBSERVER_ADDRESS = os.environ.get("SUPERSET_WEBSERVER_ADDRESS", "0.0.0.0")  # noqa: S104
SUPERSET_WEBSERVER_PORT = os.environ.get("SUPERSET_WEBSERVER_PORT", "8088")
ENABLE_PROXY_FIX = os.environ.get("ENABLE_PROXY_FIX", "True").lower() == "true"
# -------------------------------
# White Labeling Configurations #
# --------------------------------
APP_NAME = "MIT OL Business Intelligence"
# Specify the App icon. Useful for white-labeling
APP_ICON = "/static/assets/images/ol-data-platform-logo.svg"

# ----------------------------------------------------
# AUTHENTICATION CONFIG
# ----------------------------------------------------
# The authentication type
# AUTH_OID : Is for OpenID
# AUTH_DB : Is for database (username/password)
# AUTH_LDAP : Is for LDAP
# AUTH_REMOTE_USER : Is for using REMOTE_USER from web server
# AUTH_TYPE = AUTH_OID
AUTH_TYPE = AUTH_OAUTH
OAUTH_PROVIDERS = [
    {
        "name": "keycloak",
        "icon": "fa-key",
        "token_key": "access_token",
        "remote_app": {
            "client_id": OIDC_CLIENT_ID,
            "client_secret": OIDC_CLIENT_SECRET,
            "client_kwargs": {
                "scope": "openid profile email roles",
            },
            "server_metadata_url": f"{OIDC_URL}/.well-known/openid-configuration",
            "api_base_url": f"{OIDC_URL}/protocol/",
        },
    }
]

# ----------------------------------------------------------
# Required config to get Keycloak token for Superset API use
# ----------------------------------------------------------
JWT_ALGORITHM = "RS256"
JWT_PUBLIC_KEY = OIDC_REALM_PUBLIC_KEY

# Map Keycloak realm roles to Superset roles.
# ol_platform_admin → built-in Admin (full privileges).
# All other roles map to custom roles loaded from ol_governance_roles.json via
# `flask fab import-roles` during deployment init.
# See src/ol_infrastructure/applications/superset/ol_governance_roles.json
# https://superset.apache.org/docs/installation/configuring-superset#mapping-ldap-or-oauth-groups-to-superset-roles
AUTH_ROLES_MAPPING = {
    "ol_platform_admin": ["Admin"],
    "ol_data_engineer": ["ol_data_engineer"],
    "ol_data_analyst": ["ol_data_analyst"],
    "ol_researcher": ["ol_researcher"],
    "ol_instructor": ["ol_instructor"],
    "ol_business_analyst": ["ol_business_analyst"],
}

# if we should replace ALL the user's roles each login, or only on registration
AUTH_ROLES_SYNC_AT_LOGIN = True

# Will allow user self registration, allowing to create Flask users from Authorized User
AUTH_USER_REGISTRATION = True

# The default user self registration role
AUTH_USER_REGISTRATION_ROLE = "Public"


class CustomSsoSecurityManager(SupersetSecurityManager):
    def oauth_user_info(self, provider, response=None):  # noqa: ARG002
        me = self.appbuilder.sm.oauth_remotes[provider].get("openid-connect/userinfo")
        me.raise_for_status()
        data = me.json()
        logging.debug("User info from Keycloak: %s", data)
        return {
            "username": data.get("preferred_username", ""),
            "first_name": data.get("given_name", ""),
            "last_name": data.get("family_name", ""),
            "email": data.get("email", ""),
            "role_keys": data.get("role_keys", []),
        }

    @staticmethod
    def _expand_role_keys(role_keys: list[str]) -> list[str]:
        """Expand Keycloak role keys through AUTH_ROLES_MAPPING.

        Applied consistently in both OAuth and JWT provisioning paths so that
        a role like ``ol_platform_admin`` is expanded to ``["Admin"]`` regardless
        of which authentication flow the user arrives through.

        Any key not present in AUTH_ROLES_MAPPING is passed through unchanged,
        which allows Superset role names to be placed directly in tokens when
        needed.
        """
        expanded: list[str] = []
        for role_key in role_keys:
            mapped = AUTH_ROLES_MAPPING.get(role_key)
            if mapped:
                expanded.extend(mapped)
            else:
                expanded.append(role_key)
        return expanded

    def _get_roles_from_keycloak_roles(self, role_keys: list[str]) -> list[object]:
        """Map Keycloak role_keys to Superset roles.

        Args:
            role_keys: List of Keycloak role keys from JWT token

        Returns:
            List of Superset role objects
        """
        roles = []

        # Default to Public role if no role_keys provided
        if not role_keys:
            public_role = self.find_role("Public")
            if public_role:
                roles.append(public_role)

        # Expand through AUTH_ROLES_MAPPING so the JWT path is consistent with
        # the OAuth path (which FAB expands via AUTH_ROLES_MAPPING automatically).
        # Use dict.fromkeys to deduplicate while preserving order — Keycloak can
        # emit the same client role twice when it is assigned both directly and
        # via a composite realm role.
        for superset_role_name in dict.fromkeys(self._expand_role_keys(role_keys)):
            superset_role = self.find_role(superset_role_name)
            if superset_role:
                roles.append(superset_role)

        return roles

    def _create_user_from_jwt(self, jwt_data: dict[str, object]) -> object | None:
        """Create new Superset user from JWT claims (dynamic provisioning).

        This method is called when a valid JWT is received but the user does not
        yet exist in the Superset database. It automatically creates the user
        with roles derived from the JWT's role_keys claim.

        Args:
            jwt_data: Decoded JWT token data

        Returns:
            The newly created user object, or None if creation failed
        """
        username = str(jwt_data.get("preferred_username", ""))
        email = str(jwt_data.get("email", ""))
        first_name = str(jwt_data.get("given_name", ""))
        last_name = str(jwt_data.get("family_name", ""))
        role_keys_obj = jwt_data.get("role_keys", [])
        role_keys = list(role_keys_obj) if isinstance(role_keys_obj, list) else []

        if not username:
            logging.error("Cannot create user: JWT missing 'preferred_username' claim")
            return None

        try:
            # Get roles from Keycloak role_keys claim.
            # Service accounts carry role_keys in their client-credentials JWT
            # (the ol_platform_admin client role is assigned to the service account
            # in Keycloak, surfaced via UserClientRoleProtocolMapper), so they go
            # through the same path as regular users.
            roles = self._get_roles_from_keycloak_roles(role_keys)

            # Ensure at least one role
            if not roles:
                logging.warning(
                    "User '%s' has no roles from JWT, assigning Public role", username
                )
                roles = [self.find_role("Public")]

            # Create the user
            logging.info(
                "Creating new user from JWT: username=%s, email=%s, roles=%s",
                username,
                email,
                [getattr(r, "name", str(r)) for r in roles if r],
            )

            user = self.add_user(
                username=username,
                first_name=first_name or username,
                last_name=last_name or "",
                email=email or f"{username}@example.com",
                role=roles,
            )

            if not user:
                return None

            logging.info("Successfully created user '%s'", username)
            return user  # noqa: TRY300

        except Exception:
            logging.exception(
                "Failed to auto-create user '%s' from JWT",
                username,
            )
            return None

    def load_user_jwt(self, _jwt_header, jwt_data):
        """
        Load or create user from JWT token claims.

        This method is called when API access uses JWT tokens. It:
        1. Looks up the user in Superset database by preferred_username
        2. If user doesn't exist, automatically creates them from JWT claims
        3. Maps Keycloak roles to Superset permissions via role_keys claim
        4. Returns the user if active, None otherwise

        Args:
            _jwt_header: JWT header (unused)
            jwt_data: Decoded JWT token data

        Returns:
            The user object if found/created and active, None otherwise
        """
        username = jwt_data.get("preferred_username")

        if not username:
            logging.error("JWT missing 'preferred_username' claim")
            return None

        # Try to find existing user
        user = self.find_user(username=username)

        # If user doesn't exist, create them dynamically from JWT claims
        if user is None:
            logging.info(
                "User '%s' not found in database; attempting to create from JWT",
                username,
            )
            user = self._create_user_from_jwt(jwt_data)
        elif user.is_active:
            # Sync roles on every JWT request, mirroring AUTH_ROLES_SYNC_AT_LOGIN
            # for the OAuth path. This ensures role changes in Keycloak (including
            # granting the service account ol_platform_admin) take effect without
            # requiring the user record to be deleted and recreated.
            role_keys_obj = jwt_data.get("role_keys", [])
            role_keys = list(role_keys_obj) if isinstance(role_keys_obj, list) else []
            roles = self._get_roles_from_keycloak_roles(role_keys)
            if roles:
                user.roles = roles
                self.update_user(user)

        # Return user if found/created and active
        if user and user.is_active:
            # Set flask g.user to JWT user, we can't do it on before request
            g.user = user
            return user

        if user and not user.is_active:
            logging.warning("User '%s' found but is inactive", username)

        return None


CUSTOM_SECURITY_MANAGER = CustomSsoSecurityManager
# Allow for managing users and roles via API
FAB_ADD_SECURITY_API = True

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
    # TMM 2023-11-28 - This seems like a similar cross-database query set data source
    # we've seen cause a lot of annoying bugs and bad UX in a previous BI tool.
    "ENABLE_SUPERSET_META_DB": False,
    # Set to True to replace Selenium with Playwright to execute reports and thumbnails.
    # Unlike Selenium, Playwright reports support deck.gl visualizations
    # Enabling this feature flag requires installing "playwright" pip package
    "PLAYWRIGHT_REPORTS_AND_THUMBNAILS": False,
    # Set to True to enable Jinja templating in queries in SQL Lab and Explore
    "ENABLE_TEMPLATE_PROCESSING": True,
    "ENABLE_EXTENSIONS": True,
}

LOCAL_EXTENSIONS = ["/app/extensions/nl-explorer"]


# def FLASK_APP_MUTATOR(app):
#     """Register the NL Explorer plugin (Blueprint + REST API) with Superset."""
#     try:
#         from nl_explorer.entrypoint import register

#         register(app)
#     except Exception:
#         logging.getLogger(__name__).exception("Failed to register NL Explorer plugin")


# Default configurator will consume the LOG_* settings below


# Custom logger for auditing queries. This can be used to send ran queries to a
# structured immutable store for auditing purposes. The function is called for
# every query ran, in both SQL Lab and charts/dashboards.
QUERY_LOGGER = None

# Maximum number of rows returned for any analytical database query
SQL_MAX_ROW = 500000

# Disable the Superset 6.1.0 streaming CSV export path for chart data.
# That path re-runs the query inside a new app context (no active request
# scope) which fails against Trino, yielding __STREAM_ERROR__:Export failed.
# Setting the threshold above SQL_MAX_ROW means no chart will trigger it;
# all CSV exports use the proven in-memory path instead.
# Revisit once the streaming path is validated against the Trino driver.
CSV_STREAMING_ROW_THRESHOLD = 1_000_000

# SQLALCHEMY Settings
# Values based on number of instances and DB type and connections
# Formula is LEAST({DBInstanceClassMemory/9531392},5000)
SQLALCHEMY_POOL_SIZE = 40  # Base pool size per process
SQLALCHEMY_MAX_OVERFLOW = 20  # Additional connections allowed when pool is full
SQLALCHEMY_POOL_TIMEOUT = 30  # Seconds to wait for a connection from the pool
# Postgresql paramater group sets idle_in_transaction_session_timeout to 24 hours
SQLALCHEMY_POOL_RECYCLE = 43200  # 12 hours
SQLALCHEMY_POOL_PRE_PING = True  # Enable connection validation


SUPERSET_WEBSERVER_TIMEOUT = 300  # 5 minutes timeout for HTTP requests

# Caching Settings
cache_base = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 86400,
    "CACHE_REDIS_URL": f"rediss://default:{REDIS_TOKEN}@{REDIS_HOST}:6379/0",
}
FILTER_STATE_CACHE_CONFIG = {"CACHE_KEY_PREFIX": "superset_filter_cache", **cache_base}
EXPLORE_FORM_DATA_CACHE_CONFIG = {
    "CACHE_KEY_PREFIX": "superset_form_cache",
    **cache_base,
}
CACHE_CONFIG = {"CACHE_KEY_PREFIX": "superset_metadata_cache", **cache_base}
DATA_CACHE_CONFIG = {"CACHE_KEY_PREFIX": "superset_chart_cache", **cache_base}

# Set this API key to enable Mapbox visualizations
MAPBOX_API_KEY = os.environ.get("MAPBOX_API_KEY", "")

# Adding http headers to allow iframe embedding
ENABLE_CORS = True
HTTP_HEADERS = {"X-Frame-Options": "ALLOWALL"}


class CeleryConfig:  # pylint: disable=too-few-public-methods
    # Build redis URLs from env (REDIS_HOST/PORT provided by chart, REDIS_TOKEN from
    # env)
    broker_url = f"rediss://default:{REDIS_TOKEN}@{REDIS_HOST}:{REDIS_PORT}/1?ssl_cert_reqs=optional"
    imports = ("superset.sql_lab", "superset.tasks.scheduler")
    result_backend = f"rediss://default:{REDIS_TOKEN}@{REDIS_HOST}:{REDIS_PORT}/2?ssl_cert_reqs=optional"
    worker_prefetch_multiplier = 1
    task_acks_late = True
    task_track_started = True
    task_send_sent_event = True
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
RESULTS_BACKEND = RedisCache(
    db=2,
    host=REDIS_HOST,
    key_prefix="superset_results",
    password=REDIS_TOKEN,
    port=int(REDIS_PORT),
    ssl=True,
    ssl_cert_reqs=CERT_OPTIONAL,
    username="default",
)

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


#######################
# Custom Jinja Macros #
#######################
# Temporarily add a current_user_email() macro until Superset releases that feature
# Macro function that returns the current user's email
def current_user_email() -> str | None:
    """
    Get the email (if defined) associated with the current user.

    :returns: The email
    """

    try:
        return g.user.email
    except Exception:  # noqa: BLE001
        logging.debug("Could not get email for : %s", g.user)
        return None


# Adding macros to enable usage in the jinja_context for Superset
JINJA_CONTEXT_ADDONS = {"current_user_email": current_user_email}

# ---------------------------------------------------
# NL Explorer Plugin Configuration
# ---------------------------------------------------
# Keys must match what nl_explorer.llm_service and nl_explorer.api read:
#   llm_service: cfg.get("model"), cfg.get("api_key"), cfg.get("api_base"),
#                cfg.get("max_tokens"), cfg.get("streaming")
#   api.py:      cfg.get("max_datasets_in_context")
# AWS credentials come from IRSA; region from AWS_DEFAULT_REGION env var.
# NL_EXPLORER_CONFIG = {
#     "model": os.environ.get(
#         "NL_EXPLORER_MODEL",
#         "bedrock/us.anthropic.claude-3-5-sonnet-20241022-v2:0",
#     ),
#     "max_tokens": 4096,
#     "streaming": True,
#     "max_datasets_in_context": 20,
# }


# def COMMON_BOOTSTRAP_OVERRIDES_FUNC(
#     _bootstrap_data: dict[str, object],
# ) -> dict[str, object]:
#     """Inject NL Explorer org-level config into every page's bootstrapData.

#     This data is forwarded from the parent Superset frame to the NL Explorer
#     iframe via postMessage and then included in every LLM API call, allowing
#     server-controlled instructions to be appended to the system prompt without
#     embedding them in the frontend bundle.
#     """
#     _default_suffix = (
#         "You are deployed at MIT Open Learning. "
#         "Datasets follow MIT OpenLearning naming conventions. "
#         "Prefer clear, concise answers suitable for a higher-education"
#         " analytics audience."
#     )
#     return {
#         "nl_explorer": {
#             "system_prompt_suffix": os.environ.get(
#                 "NL_EXPLORER_SYSTEM_PROMPT_SUFFIX", _default_suffix
#             ),
#         }
#     }


# ---------------------------------------------------
# Dynamic Credential Interpolation (Vault / K8s)
# ---------------------------------------------------
# Analytical databases can store ${ENV_VAR_NAME} placeholders in their
# SQLAlchemy URI (username, password, host, or database fields) instead of
# hard-coded credentials.  At connection time, DB_CONNECTION_MUTATOR resolves
# those placeholders from the process environment, where Vault Secrets Operator
# injects short-lived dynamic credentials as Kubernetes secret environment
# variables.
#
# Example SQLAlchemy URI entered in the Superset UI:
#   postgresql+psycopg2://${STARROCKS_DB_USER}:${STARROCKS_DB_PASS}@host:5432/db
#
# The corresponding env vars must be present in the container (provided via a
# VaultStaticSecret / VaultDynamicSecret K8s resource mounted as envFrom).

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env_vars(value: str | None) -> str | None:
    """Replace every ${VAR_NAME} token with os.environ[VAR_NAME].

    Tokens whose corresponding environment variable is absent are left
    unchanged and a warning is emitted so operators can diagnose
    misconfigured secret mounts quickly.
    """
    if value is None or not _ENV_VAR_PATTERN.search(value):
        return value

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            logging.warning(
                "DB_CONNECTION_MUTATOR: environment variable '%s' not found; "
                "leaving placeholder unreplaced in URI",
                var_name,
            )
            return match.group(0)
        return env_value

    return _ENV_VAR_PATTERN.sub(_replace, value)


# ---------------------------------------------------
# MCP Server Configuration
# ---------------------------------------------------
# The MCP server runs as a separate Kubernetes Deployment (superset-mcp)
# alongside the main Superset web pods.  It exposes the Superset MCP API at
# /mcp, routed through the existing Gateway listener at https://{domain}/mcp.
#
# Authentication re-uses the Keycloak RS256 JWT tokens already issued for
# Superset UI access.  The MCP server validates incoming Bearer tokens against
# the Keycloak JWKS endpoint and resolves the Superset user by
# preferred_username, which mirrors the existing
# CustomSsoSecurityManager.load_user_jwt path.
#
# Multi-pod session state is backed by Redis DB 3 (distinct from the Superset
# metadata cache on DB 0, Celery broker on DB 1, and Celery results on DB 2)
# so that all MCP replicas share session context.
#
# Requires: fastmcp package in the Superset image (pip install fastmcp).

MCP_SERVICE_HOST = "0.0.0.0"  # noqa: S104 - bind all interfaces in k8s
MCP_SERVICE_PORT = 5008
# Public-facing base URL for MCP-generated links (e.g. chart preview URLs).
# Injected at runtime via SUPERSET_MCP_PUBLIC_URL env var set in Pulumi.
MCP_SERVICE_URL = os.environ.get("SUPERSET_MCP_PUBLIC_URL")

# JWT authentication via Keycloak RS256 JWKS endpoint.
# OIDC_URL is the Keycloak realm base URL injected from the oidc k8s secret
# (e.g. https://keycloak.example.com/realms/master).
MCP_AUTH_ENABLED = True
MCP_JWT_ALGORITHM = "RS256"
# Keycloak publishes its realm signing keys at this well-known path.
MCP_JWKS_URI = f"{OIDC_URL}/protocol/openid-connect/certs" if OIDC_URL else None
# Keycloak sets the 'iss' claim to the realm URL.
MCP_JWT_ISSUER = OIDC_URL


def _mcp_user_resolver(app: object, access_token: object) -> str:  # noqa: ARG001
    """Resolve a Superset username from a validated Keycloak JWT.

    Checks claims in the same order as
    ``CustomSsoSecurityManager.load_user_jwt`` so both the UI and MCP paths
    identify the same Superset user from a given Keycloak token.
    """
    payload: dict[str, object] = getattr(access_token, "payload", {})
    return (
        str(payload.get("preferred_username") or "")
        or str(payload.get("username") or "")
        or str(payload.get("email") or "")
        or str(getattr(access_token, "subject", "") or "")
        or ""
    )


MCP_USER_RESOLVER = _mcp_user_resolver

# Redis-backed session store so all MCP replicas share session state.
# Uses Redis DB 3 — distinct from metadata cache (DB 0), Celery broker (DB 1),
# and Celery results backend (DB 2).
MCP_STORE_CONFIG = {
    "enabled": True,
    "CACHE_REDIS_URL": f"rediss://default:{REDIS_TOKEN}@{REDIS_HOST}:{REDIS_PORT}/3",
    "event_store_max_events": 100,
    "event_store_ttl": 3600,
}

# Response caching for read-heavy tool calls (dashboard/dataset listings).
# Mutating and non-deterministic tools are always excluded.
MCP_CACHE_CONFIG = {
    "enabled": True,
    "CACHE_KEY_PREFIX": "superset_mcp_cache",
    "list_tools_ttl": 300,
    "list_resources_ttl": 300,
    "list_prompts_ttl": 300,
    "read_resource_ttl": 3600,
    "get_prompt_ttl": 3600,
    "call_tool_ttl": 3600,
    "max_item_size": 1048576,  # 1 MB per cached response
    "excluded_tools": [
        "execute_sql",
        "generate_dashboard",
        "generate_chart",
        "update_chart",
    ],
}


def DB_CONNECTION_MUTATOR(  # noqa: N802
    uri: URL,
    params: dict[str, object],
    username: str,  # noqa: ARG001
    security_manager: object,  # noqa: ARG001
    source: object,  # noqa: ARG001
) -> tuple[URL, dict[str, object]]:
    """Interpolate ${ENV_VAR_NAME} placeholders in analytical DB connection URIs.

    Called by Superset immediately before each SQLAlchemy engine is created for
    an analytical (non-metadata) database.  Replaces placeholder tokens in the
    URI components with values sourced from the process environment so that
    Vault dynamic credentials (rotated by Vault Secrets Operator) are always
    current without requiring the connection string to be updated in the UI.

    Only URIs that contain at least one ${...} token incur any overhead; all
    other connections pass through unchanged.
    """
    raw_username = uri.username
    raw_password = str(uri.password) if uri.password else None
    raw_host = uri.host
    raw_database = uri.database

    components = [raw_username, raw_password, raw_host, raw_database]
    if not any(_ENV_VAR_PATTERN.search(c) for c in components if c is not None):
        return uri, params

    new_username = _interpolate_env_vars(raw_username)
    new_password = _interpolate_env_vars(raw_password)
    new_host = _interpolate_env_vars(raw_host)
    new_database = _interpolate_env_vars(raw_database)

    uri = uri.set(
        username=new_username,
        password=new_password,
        host=new_host,
        database=new_database,
    )
    return uri, params
