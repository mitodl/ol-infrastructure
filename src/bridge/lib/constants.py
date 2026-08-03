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


def mit_learn_session_cookie_name(env_suffix: str) -> str:
    """Return the APISIX OIDC session cookie name for a MIT Learn environment.

    APISIX's openid-connect plugin (lua-resty-session 4.x) names its session
    cookie ``session`` by default.  That is a problem for MIT Learn for two
    reasons:

    * It is generic enough to collide with any other ``session`` cookie a
      *.mit.edu host sets, and it says nothing about which system owns it when
      debugging an oversized/garbled Cookie header.
    * MIT Learn's session cookie is deliberately scoped to the parent domain
      (``.learn.mit.edu``) so it also reaches sibling subdomains, which means
      the Production cookie is sent to ``api.rc.learn.mit.edu`` and
      ``api.ci.learn.mit.edu`` as well.  Under a single shared name the three
      environments' cookies stack up on those hosts and the gateway can be
      handed a session envelope it cannot decrypt (each environment has its own
      session secret).  Giving every environment its own name keeps them
      distinct in the browser's cookie jar.

    Every APISIX OIDC resource that participates in the shared MIT Learn login
    session must use the same name for a given environment: mit-learn's own
    routes, learn-ai's ``/ai/*`` routes and ol-analytics-api's Learn-scoped
    host all share the ``sso/mitlearn`` Keycloak client and read the cookie
    mit-learn set, so a mismatch silently turns their ``unauth_action="pass"``
    routes into anonymous ones.

    :param env_suffix: The lowercase stack environment suffix, e.g. ``"qa"``.

    :returns: The session cookie name for that environment.
    :rtype: str
    """
    return f"mitlearn_session_{env_suffix.lower()}"
