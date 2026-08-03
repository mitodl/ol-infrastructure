from ipaddress import IPv4Address

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
