# Not an importable module: tutor discovers single-file plugins by path from its
# own plugins root, so there is no package for this to belong to.
# ruff: noqa: INP001
"""
Tutor plugin: publish the `tutor dev` LMS/CMS through the local-dev ingress.

Installed (as a symlink) into `tutor plugins printroot` and enabled by
local-dev/scripts/tutor-configure.sh. Edits here take effect on the next
`tutor config save`, which that script runs.

Why it is needed: tutor's dev settings hardcode `http://{{ LMS_HOST }}:8000`
and `http://{{ CMS_HOST }}:8001` for every self-referential URL, because
`tutor dev` normally expects you to hit the runserver ports directly. In this
stack the ports are an implementation detail — APISIX terminates TLS and
serves Open edX at https://lms.<root_domain> / https://studio.<root_domain>,
the same shape as every other app. These patches rewrite those URLs and make
Django trust APISIX's X-Forwarded-Proto so it builds https links and accepts
secure cookies.

Every patch below lands at the *end* of the rendered settings file (tutor
calls `{{ patch(...) }}` last), so these assignments win over the defaults.
"""

from tutor import hooks

# Root domain of the local-dev stack (LOCAL_DEV_ROOT_DOMAIN). Only used to
# whitelist the other apps' origins; the Open edX hostnames themselves come
# from tutor's own LMS_HOST/CMS_HOST/PREVIEW_LMS_HOST.
hooks.Filters.CONFIG_DEFAULTS.add_item(("OL_LOCAL_DEV_ROOT_DOMAIN", "mit.dev"))

# Whether anonymous requests are pushed straight into the mitxonline SSO flow
# (see MITX_REDIRECT_ENABLED below).
hooks.Filters.CONFIG_DEFAULTS.add_item(("OL_LOCAL_DEV_FORCE_SSO", True))

# Studio's preview host. Tutor stopped shipping a PREVIEW_LMS_HOST default of
# its own, so this stack owns the value: APISIX routes preview.lms.<root_domain>
# (local-dev/apps/openedx-tutor/apisix-routes.yaml) and the patches below feed
# it to FEATURES["PREVIEW_LMS_BASE"] and the session-cookie domain.
#
# It has to be declared here rather than only set by tutor-configure.sh, because
# `tutor plugins enable` renders the environment the moment this plugin becomes
# active — before that script's `tutor config save --set` block runs. A patch
# below referencing a value with no default therefore fails on any tutor root
# whose config.yml does not already carry it, which is every freshly reset one.
# Anything new these patches interpolate needs a default here for the same
# reason. tutor-configure.sh's explicit --set still wins over this.
hooks.Filters.CONFIG_DEFAULTS.add_item(("PREVIEW_LMS_HOST", "preview.{{ LMS_HOST }}"))

# Shared by LMS and CMS: TLS is terminated at the ingress, so the runserver
# only ever sees plain HTTP and has to be told what the browser used.
BEHIND_INGRESS = """
# --- ol-infrastructure local-dev ---------------------------------------------
# APISIX terminates TLS in front of this process.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
"""

OL_ORIGINS = """
_OL_LOCAL_DEV_HOSTS = [
    "{{ LMS_HOST }}",
    "{{ CMS_HOST }}",
    "{{ PREVIEW_LMS_HOST }}",
    "mitxonline.{{ OL_LOCAL_DEV_ROOT_DOMAIN }}",
    "learn.{{ OL_LOCAL_DEV_ROOT_DOMAIN }}",
    "api.learn.{{ OL_LOCAL_DEV_ROOT_DOMAIN }}",
]
_OL_LOCAL_DEV_ORIGINS = ["https://" + _host for _host in _OL_LOCAL_DEV_HOSTS]

CORS_ORIGIN_WHITELIST = list(CORS_ORIGIN_WHITELIST) + _OL_LOCAL_DEV_ORIGINS
CSRF_TRUSTED_ORIGINS = list(CSRF_TRUSTED_ORIGINS) + _OL_LOCAL_DEV_ORIGINS
LOGIN_REDIRECT_WHITELIST = list(LOGIN_REDIRECT_WHITELIST) + _OL_LOCAL_DEV_HOSTS
"""

# tutor-mfe, when enabled, points every micro-frontend URL at
# http://<MFE_HOST>:<port>/<name> in dev mode, because each MFE normally runs
# its own dev server on its own host port. Behind the ingress they are all
# reachable at https://<MFE_HOST>/<name> instead (APISIX routes the path to the
# right port — see local-dev/apps/openedx-tutor/apisix-routes.yaml), which is
# the shape tutor-mfe already uses in production. Without this the LMS would
# send browsers to ports that are not in the developer's /etc/hosts and are not
# covered by the TLS certificate, and login — the authn MFE — would be the
# first thing to break.
#
# Mirrors tutormfe/patches/openedx-lms-production-settings; an MFE added
# upstream after this was written keeps the dev URL until it is listed here.
MFE_URLS = """
# --- ol-infrastructure local-dev ---------------------------------------------
MFE_CONFIG["BASE_URL"] = "{{ MFE_HOST }}"
MFE_CONFIG["LMS_BASE_URL"] = "https://{{ LMS_HOST }}"
MFE_CONFIG["LOGIN_URL"] = "https://{{ LMS_HOST }}/login"
MFE_CONFIG["LOGOUT_URL"] = "https://{{ LMS_HOST }}/logout"
MFE_CONFIG["REFRESH_ACCESS_TOKEN_ENDPOINT"] = "https://{{ LMS_HOST }}/login_refresh"
MFE_CONFIG["MARKETING_SITE_BASE_URL"] = "https://{{ LMS_HOST }}"
MFE_CONFIG["FAVICON_URL"] = "https://{{ LMS_HOST }}/favicon.ico"
MFE_CONFIG["LOGO_URL"] = "https://{{ LMS_HOST }}/theming/asset/images/logo.png"
MFE_CONFIG["LOGO_WHITE_URL"] = MFE_CONFIG["LOGO_URL"]
MFE_CONFIG["LOGO_TRADEMARK_URL"] = MFE_CONFIG["LOGO_URL"]
MFE_CONFIG["STUDIO_BASE_URL"] = "https://{{ CMS_HOST }}"

{% if get_mfe("authn") %}
AUTHN_MICROFRONTEND_URL = "https://{{ MFE_HOST }}/authn"
AUTHN_MICROFRONTEND_DOMAIN = "{{ MFE_HOST }}/authn"
{% endif %}
{% if get_mfe("account") %}
ACCOUNT_MICROFRONTEND_URL = "https://{{ MFE_HOST }}/account/"
MFE_CONFIG["ACCOUNT_SETTINGS_URL"] = ACCOUNT_MICROFRONTEND_URL
{% endif %}
{% if get_mfe("authoring") %}
MFE_CONFIG["COURSE_AUTHORING_MICROFRONTEND_URL"] = "https://{{ MFE_HOST }}/authoring"
{% endif %}
{% if get_mfe("discussions") %}
DISCUSSIONS_MICROFRONTEND_URL = "https://{{ MFE_HOST }}/discussions"
MFE_CONFIG["DISCUSSIONS_MFE_BASE_URL"] = DISCUSSIONS_MICROFRONTEND_URL
{% endif %}
{% if get_mfe("gradebook") %}
WRITABLE_GRADEBOOK_URL = "https://{{ MFE_HOST }}/gradebook"
{% endif %}
{% if get_mfe("learner-dashboard") %}
LEARNER_HOME_MICROFRONTEND_URL = "https://{{ MFE_HOST }}/learner-dashboard/"
{% endif %}
{% if get_mfe("learning") %}
LEARNING_MICROFRONTEND_URL = "https://{{ MFE_HOST }}/learning"
MFE_CONFIG["LEARNING_BASE_URL"] = "https://{{ MFE_HOST }}/learning"
{% endif %}
{% if get_mfe("ora-grading") %}
ORA_GRADING_MICROFRONTEND_URL = "https://{{ MFE_HOST }}/ora-grading"
{% endif %}
{% if get_mfe("profile") %}
PROFILE_MICROFRONTEND_URL = "https://{{ MFE_HOST }}/profile/u/"
MFE_CONFIG["ACCOUNT_PROFILE_URL"] = "https://{{ MFE_HOST }}/profile"
{% endif %}
{% if get_mfe("communications") %}
COMMUNICATIONS_MICROFRONTEND_URL = "https://{{ MFE_HOST }}/communications"
{% endif %}

LOGIN_REDIRECT_WHITELIST = list(LOGIN_REDIRECT_WHITELIST) + ["{{ MFE_HOST }}"]
CORS_ORIGIN_WHITELIST = list(CORS_ORIGIN_WHITELIST) + ["https://{{ MFE_HOST }}"]
CSRF_TRUSTED_ORIGINS = list(CSRF_TRUSTED_ORIGINS) + ["https://{{ MFE_HOST }}"]
"""

hooks.Filters.ENV_PATCHES.add_items(
    [
        (
            "openedx-lms-development-settings",
            BEHIND_INGRESS
            + """
LMS_BASE = "{{ LMS_HOST }}"
LMS_ROOT_URL = "https://{{ LMS_HOST }}"
LMS_INTERNAL_ROOT_URL = LMS_ROOT_URL
SITE_NAME = LMS_BASE
CMS_BASE = "{{ CMS_HOST }}"
CMS_ROOT_URL = "https://{{ CMS_HOST }}"
FEATURES["PREVIEW_LMS_BASE"] = "{{ PREVIEW_LMS_HOST }}"

# Leading dot so the preview host (preview.{{ LMS_HOST }}) gets the session
# cookie too. Deliberately not widened to the root domain: mitxonline and
# mit-learn are Django apps on sibling subdomains using the same cookie name.
SESSION_COOKIE_DOMAIN = ".{{ LMS_HOST }}"

# Front-channel logout. The LMS's logout page renders one hidden iframe per URI
# here, with ?no_redirect=1 appended, so signing out of Open edX signs the user
# out of the whole stack. Each app's own /logout clears its Django session and
# then hops into its APISIX /logout/oidc, which ends the Keycloak session with
# an id_token_hint — so Keycloak needs no entry of its own, it is the last stop
# of every chain. Same list as production (IDA_LOGOUT_URI_LIST in
# src/ol_infrastructure/applications/edxapp/k8s_configmaps.py), including the
# bare /logout paths: adding /logout/oidc directly would skip the app session.
#
# An entry for an app that is not running resolves to APISIX all the same and
# comes back 404, which the logout page treats as a loaded iframe.
#
# Assigned, not appended, exactly as production assigns it. Everything already
# in the list is wrong here and would only make logout slower: devstack seeds it
# with localhost:181xx services this stack does not run, and tutor's own dev
# settings add Studio on its runserver port, which the ingress has replaced.
IDA_LOGOUT_URI_LIST = [
    "https://mitxonline.{{ OL_LOCAL_DEV_ROOT_DOMAIN }}/logout",
    "https://{{ CMS_HOST }}/logout",
    "https://api.learn.{{ OL_LOCAL_DEV_ROOT_DOMAIN }}/logout",
]

# SSO against mitxonline, the way deployed environments do it: ol-social-auth
# registers the ol-oauth2 backend, and mitxonline is the identity provider
# (which in turn sits behind Keycloak).
#
# AUTHENTICATION_BACKENDS is the only list that matters, and prepending to it
# here is enough: third-party auth resolves a provider row's backend_name
# against it (common/djangoapps/third_party_auth/models.py), and nothing reads
# THIRD_PARTY_AUTH_BACKENDS at runtime. That name is not a setting at all - it
# is a key production.py looks for in the LMS_CFG yaml *while building*
# AUTHENTICATION_BACKENDS (lms/envs/production.py), so it does not survive into
# the settings module, and touching it from here would be both a NameError and
# too late to have any effect.
_OL_SSO_BACKEND = "ol_social_auth.backends.OLOAuth2"
if _OL_SSO_BACKEND not in AUTHENTICATION_BACKENDS:
    AUTHENTICATION_BACKENDS = [_OL_SSO_BACKEND] + list(AUTHENTICATION_BACKENDS)

# The provider's endpoints and credentials live in an OAuth2ProviderConfig row
# created by local-dev/scripts/tutor-seed-sso.sh, not here: third-party auth
# reads them from the database, and the row has to reference the Site that
# SITE_ID resolves to.

# openedx-companion-auth, when installed, sends every anonymous request to the
# SSO login instead of Open edX's own. That is what deployed environments do,
# so it is on by default — but it means an Open edX with no reachable
# mitxonline can only be logged into through /admin (the middleware's allow
# list also covers /auth, /login, /register, /api, /oauth2 and /user_api). Set
# OL_LOCAL_DEV_FORCE_SSO to false to get the built-in login page back:
#   tutor config save --set OL_LOCAL_DEV_FORCE_SSO=false
MITX_REDIRECT_ENABLED = {{ "True" if OL_LOCAL_DEV_FORCE_SSO else "False" }}
MITX_REDIRECT_LOGIN_URL = "/auth/login/ol-oauth2/?auth_entry=login"
"""
            + OL_ORIGINS,
        ),
        (
            "openedx-cms-development-settings",
            BEHIND_INGRESS
            + """
LMS_BASE = "{{ LMS_HOST }}"
LMS_ROOT_URL = "https://{{ LMS_HOST }}"
CMS_BASE = "{{ CMS_HOST }}"
CMS_ROOT_URL = "https://{{ CMS_HOST }}"
SITE_NAME = CMS_BASE
FEATURES["PREVIEW_LMS_BASE"] = "{{ PREVIEW_LMS_HOST }}"
FRONTEND_LOGIN_URL = LMS_ROOT_URL + "/login"

# Sign out of Studio through the LMS, so the logout fans out to the whole stack
# (IDA_LOGOUT_URI_LIST in the LMS settings, which iframes Studio's own /logout
# back to clear this session). Studio cannot fan out on its own: it resolves
# logout.html through Mako rather than Django, so the page comes back with its
# template tags unrendered and neither the iframes nor the redirect script
# exist. Its /logout still ends the session, which is all the LMS needs of it.
# Mirrors the FRONTEND_LOGIN_URL tutor already points at the LMS.
FRONTEND_LOGOUT_URL = LMS_ROOT_URL + "/logout"
# Emptied for the same reason, and as production leaves it: what devstack seeds
# it with is a set of localhost:181xx services this stack does not run.
IDA_LOGOUT_URI_LIST = []

# Studio logs in through the LMS over OAuth2. The dev default uses the
# cms-sso-dev application, whose redirect URI is http://{{ CMS_HOST }}:8001/...;
# behind the ingress the browser lands on https://{{ CMS_HOST }}/... instead,
# which is exactly the cms-sso application tutor's own init task creates when
# ENABLE_HTTPS is on. SOCIAL_AUTH_EDX_OAUTH2_URL_ROOT stays container-internal.
SOCIAL_AUTH_EDX_OAUTH2_KEY = "{{ CMS_OAUTH2_KEY_SSO }}"
SOCIAL_AUTH_EDX_OAUTH2_SECRET = "{{ CMS_OAUTH2_SECRET }}"
SOCIAL_AUTH_EDX_OAUTH2_PUBLIC_URL_ROOT = LMS_ROOT_URL
"""
            + OL_ORIGINS,
        ),
        # Only rendered when tutor-mfe is enabled, so the MFE_CONFIG references
        # above are always safe here.
        ("mfe-lms-development-settings", MFE_URLS),
        (
            "openedx-cms-development-settings",
            """
{% if get_mfe is defined and get_mfe("authoring") %}
COURSE_AUTHORING_MICROFRONTEND_URL = "https://{{ MFE_HOST }}/authoring"
CORS_ORIGIN_WHITELIST = list(CORS_ORIGIN_WHITELIST) + ["https://{{ MFE_HOST }}"]
CSRF_TRUSTED_ORIGINS = list(CSRF_TRUSTED_ORIGINS) + ["https://{{ MFE_HOST }}"]
LOGIN_REDIRECT_WHITELIST = list(LOGIN_REDIRECT_WHITELIST) + ["{{ MFE_HOST }}"]
{% endif %}
""",
        ),
        # Applies only to an MFE whose repo the developer has mounted for
        # development: its dev server proxies the config API to the LMS, and
        # the stock target (http://<LMS_HOST>:8000) is not reachable from
        # inside the compose network now that the port is an internal detail.
        (
            "mfe-webpack-dev-config",
            """
// ol-infrastructure local-dev
module.exports.devServer.proxy["/api/mfe_config/v1"].target = "http://lms:8000";
""",
        ),
    ]
)
