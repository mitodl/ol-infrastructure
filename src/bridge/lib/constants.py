from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from types import MappingProxyType
from typing import ClassVar

FASTLY_CNAME_TLS_1_2 = "d.sni.global.fastly.net"
FASTLY_CNAME_TLS_1_3 = "j.sni.global.fastly.net"
FASTLY_A_TLS_1_2 = [
    IPv4Address("151.101.2.133"),
    IPv4Address("151.101.66.133"),
    IPv4Address("151.101.130.133"),
    IPv4Address("151.101.194.133"),
]
FASTLY_A_TLS_1_3 = [
    IPv4Address("151.101.2.132"),
    IPv4Address("151.101.66.132"),
    IPv4Address("151.101.130.132"),
    IPv4Address("151.101.194.132"),
]


# The cookie name lua-resty-session 4.x -- and therefore APISIX's
# openid-connect plugin -- uses when nothing sets ``session.cookie_name``.
# Every application that has since moved to an explicit name left one of these
# behind in its users' browsers; see ``stale_session_cookie_cleanup_plugin`` in
# ol_infrastructure.components.services.apisix for how they get evicted.
DEFAULT_OIDC_SESSION_COOKIE_NAME = "session"  # pragma: allowlist secret


# The request headers APISIX's openid-connect plugin mints once it holds a
# verified session.  They are assertions the gateway makes about the caller, so
# a copy arriving from a client is always a forgery -- mitol-apigateway's
# middleware authenticates off X-Userinfo without asking where it came from.
# The plugin clears inbound copies itself, but only on the routes it is
# attached to; see ``gateway_global_pre_function_plugin`` in
# ol_infrastructure.components.services.apisix for the cluster-wide rule that
# covers the rest.
#
# This is openid-connect.lua's own clear-then-set list (3.18.0, lines 1174-1177
# and 1482-1500) minus one entry.  X-Access-Token is *not* here even though the
# plugin sets it: Tika's route uses that header name for a client-supplied
# shared secret (applications/tika/__main__.py) which mit-learn sends on every
# extraction request, and no application reads it as an identity claim.
# Authorization is out for the same reason, on a much larger scale -- it carries
# real API credentials on most of the fleet.
#
# Dash spellings only.  The underscore alias of each (``X_Userinfo``) has to be
# cleared too -- APISIX runs nginx with ``underscores_in_headers on`` and Django
# folds both spellings onto the same ``HTTP_X_USERINFO`` -- but that is a
# transport alias of the same header rather than a header of its own, so the
# Lua derives it instead of it being listed here twice.
GATEWAY_IDENTITY_HEADERS = (
    "X-Userinfo",
    "X-ID-Token",
    "X-Raw-ID-Token",
    "X-Refresh-Token",
)


def apisix_oidc_session_cookie_name(application: str, env_suffix: str) -> str:
    """Return the APISIX OIDC session cookie name for an application/environment.

    APISIX's openid-connect plugin (lua-resty-session 4.x) names its session
    cookie ``session`` by default.  That is a poor default for us for two
    reasons:

    * It is generic enough to collide with any other ``session`` cookie a
      *.mit.edu host sets, and it says nothing about which system owns it when
      debugging an oversized/garbled Cookie header.
    * Several of our session cookies are deliberately scoped to a parent domain
      (``.learn.mit.edu``, ``.mitxonline.mit.edu``) so they reach sibling
      subdomains, which means the Production cookie is also sent to the RC and
      CI hosts under that parent.  Under a single shared name the three
      environments' cookies stack up on those hosts and the gateway can be
      handed a session envelope it cannot decrypt (each environment has its own
      session secret).  Giving every environment its own name keeps them
      distinct in the browser's cookie jar.

    The ``apisix`` segment names the gateway as the owner.  The application
    behind it sets cookies of its own on the same host -- mit-learn's Django
    app is the obvious case -- and when the question in front of you is which
    component wrote an oversized Cookie header, ``mitlearn_apisix_session``
    answers it and ``mitlearn_session`` does not.

    Production is unsuffixed (``mitlearn_apisix_session``) to match how the
    rest of the codebase names user-visible, environment-scoped strings --
    compare the ``learn_csrftoken`` / ``learn_rc_csrftoken`` pair.  That is
    safe precisely because the non-production names *are* suffixed: a
    Production ``.learn.mit.edu`` cookie riding along to
    ``api.rc.learn.mit.edu`` is called ``mitlearn_apisix_session`` there, which
    is not the name the RC gateway reads (``mitlearn_apisix_session_qa``), so
    the two never contend.

    :param application: Slug of the application owning the session, e.g.
        ``"mitlearn"``.  Hyphens are normalised to underscores.
    :param env_suffix: The stack environment suffix, e.g. ``"qa"``.

    :returns: The session cookie name for that application and environment.
    :rtype: str
    """
    base = f"{application.replace('-', '_')}_apisix_session"
    env = env_suffix.lower()
    return base if env == "production" else f"{base}_{env}"


def mit_learn_session_cookie_name(env_suffix: str) -> str:
    """Return the APISIX OIDC session cookie name for a MIT Learn environment.

    Every APISIX OIDC resource that participates in the shared MIT Learn login
    session must use the same name for a given environment, so they all call
    this rather than building the name themselves:

    * mit-learn's own prefixed and un-prefixed routes on
      ``api.<env>.learn.mit.edu``, which perform the login,
    * learn-ai's ``/ai/*`` routes, served from that same host,
    * ol-analytics-api's Learn-scoped host, and
    * mitxonline's ``/mitxonline/*`` routes, which are served from
      ``api.<env>.learn.mit.edu`` too and are what the MIT Learn frontend calls
      as ``NEXT_PUBLIC_MITX_ONLINE_BASE_URL``.

    All of them share the ``sso/mitlearn`` Keycloak client (and therefore the
    session secret) and read the cookie mit-learn's login flow set, so a
    mismatch silently turns their ``unauth_action="pass"`` routes into
    anonymous ones.  mitxonline's *other* OIDC resource -- the one behind its
    own ``*.mitxonline.mit.edu`` login -- is a separate session on a separate
    parent domain and deliberately does not use this name.

    :param env_suffix: The stack environment suffix, e.g. ``"qa"``.

    :returns: The shared MIT Learn session cookie name for that environment.
    :rtype: str
    """
    return apisix_oidc_session_cookie_name("mitlearn", env_suffix)


@dataclass(frozen=True)
class LearnSession:
    """The one APISIX OIDC session shared by every route group on MIT Learn's hosts.

    mit-learn's own routes, learn-ai's ``/ai/*``, mitxonline's ``/mitxonline/*``
    and ol-analytics-api's Learn-scoped host all read (and some write) the same
    session cookie, so every setting that decides whether a cookie written by one
    of them is accepted by another lives here rather than in each stack.  A
    change to any of them has to reach all of those stacks at once or users are
    silently logged out of the routes that lag behind.

    Only the environment and the parent domain vary.  Everything else is a class
    attribute so that no stack can override it for its own route group.

    :param env_suffix: The stack environment suffix, e.g. ``"qa"``.
    :param cookie_domain: The parent domain the session cookie is scoped to,
        with its leading dot, e.g. ``".rc.learn.mit.edu"``.
    """

    env_suffix: str
    cookie_domain: str

    # The Keycloak client (ol-mitlearn-client) every participant shares, and so
    # the session.secret the cookie is encrypted with.
    vault_path: ClassVar[str] = "sso/mitlearn"
    # 14 days, matching the Keycloak SSO session.  Idling and rolling are off so
    # the envelope lasts the full absolute timeout instead of lua-resty-session's
    # compiled-in 15min/60min (hq#8416).
    absolute_timeout_seconds: ClassVar[int] = 14 * 24 * 60 * 60
    idling_timeout_seconds: ClassVar[int] = 0
    rolling_timeout_seconds: ClassVar[int] = 0
    # organization:* is what mit-learn maps to users.User.organizations through
    # APISIX_USERDATA_MAP, so any route group that performs a login has to
    # request it or the user it writes into the shared session lacks it.
    scope: ClassVar[str] = "openid profile email organization:*"
    session_contents: ClassVar[Mapping[str, bool]] = MappingProxyType(
        {"access_token": True, "enc_id_token": True, "id_token": True, "user": True}
    )

    @classmethod
    def for_host(cls, env_suffix: str, host: str) -> "LearnSession":
        """Return the session for a host directly under MIT Learn's parent domain.

        Any sibling works (``api.rc.learn.mit.edu``,
        ``analytics.rc.learn.mit.edu``): the cookie domain is the host minus its
        first label.

        :param env_suffix: The stack environment suffix, e.g. ``"qa"``.
        :param host: A host one label below the Learn parent domain.
        :returns: The shared session for that environment.
        :rtype: LearnSession
        """
        _, separator, parent = host.partition(".")
        if not separator or not parent:
            msg = f"{host!r} has no parent domain to scope the Learn session to"
            raise ValueError(msg)
        return cls(env_suffix=env_suffix, cookie_domain=f".{parent}")

    @property
    def cookie_name(self) -> str:
        """The session cookie's name in this environment."""
        return mit_learn_session_cookie_name(self.env_suffix)

    @property
    def api_host(self) -> str:
        """The host mit-learn's own login and logout views are served from."""
        return f"api{self.cookie_domain}"

    @property
    def logout_landing_uri(self) -> str:
        """Where a logout of the shared session should end up.

        mit-learn's CustomLogoutView clears its Django session (which otherwise
        outlives the gateway session) and sends the browser on to the Learn
        frontend.  A route group whose application has no logout view of its own
        uses this as its post-logout redirect.
        """
        return f"https://{self.api_host}/logout/"


# The fields of a Keycloak client's credentials in Vault
# (secret-operations/sso/<client>) that the openid-connect plugin needs. `url`
# is the realm URL (<keycloak>/realms/<realm>), not Keycloak's base URL: the
# discovery document hangs off the realm.
OIDC_VAULT_FIELDS = ("client_id", "client_secret", "realm_name", "url", "secret")


def oidc_secret_data(
    vault_fields: Mapping[str, str], *, include_session_secret: bool
) -> dict[str, str]:
    """Return the keys APISIX's openid-connect plugin reads from its ``secretRef``.

    Given VSO template expressions for ``OIDC_VAULT_FIELDS`` it returns the
    templates VSO renders the Secret from; given the fields' values it returns
    the Secret's data. The values have to be plain strings: resolve Pulumi
    Outputs first (inside an ``apply``), or the f-string below formats the
    Output object instead of its value.

    :param vault_fields: Each name in ``OIDC_VAULT_FIELDS`` mapped to its value.
    :param include_session_secret: Add ``session.secret``, the key
        lua-resty-session encrypts the session cookie with.
    :returns: Plugin config keys mapped to their values.
    """
    data = {
        "client_id": vault_fields["client_id"],
        "client_secret": vault_fields["client_secret"],
        "realm": vault_fields["realm_name"],
        "discovery": f"{vault_fields['url']}/.well-known/openid-configuration",
    }
    if include_session_secret:
        data["session.secret"] = vault_fields["secret"]
    return data
