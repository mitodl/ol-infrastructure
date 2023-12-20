import logging
from urllib.parse import quote

from flask import redirect, request, session
from flask_appbuilder.security.manager import AUTH_OID
from flask_appbuilder.security.views import AuthOIDView
from flask_appbuilder.views import expose
from flask_login import login_user
from flask_oidc import OpenIDConnect
from superset.security import SupersetSecurityManager

logger = logging.getLogger()


class OIDCSecurityManager(SupersetSecurityManager):
    def __init__(self, appbuilder):
        super().__init__(appbuilder)
        if self.auth_type == AUTH_OID:
            self.oid = OpenIDConnect(self.appbuilder.get_app)
        self.authoidview = AuthOIDCView


# Reference: https://superset.apache.org/docs/security/
class AuthOIDCView(AuthOIDView):
    @expose("/login/", methods=["GET", "POST"])
    def login(self, flag=True):  # noqa: FBT002, ARG002
        sm = self.appbuilder.sm
        oidc = sm.oid
        superset_roles = ["Admin", "Alpha", "Gamma", "Public", "granter", "sql_lab"]
        default_role = "Alpha"

        @self.appbuilder.sm.oid.require_login
        def handle_login():
            user = sm.auth_user_oid(oidc.user_getfield("email"))
            if user is None:
                info = oidc.user_getinfo(
                    ["sub", "given_name", "family_name", "email", "roles"]
                )
                roles = [
                    role for role in superset_roles if role in info.get("roles", [])
                ]
                roles += (
                    [
                        default_role,
                    ]
                    if not roles
                    else []
                )
                user = sm.add_user(
                    username=info.get("sub"),
                    first_name=info.get("given_name"),
                    last_name=info.get("family_name"),
                    email=info.get("email"),
                    role=[sm.find_role(role) for role in roles],
                )

            login_user(user, remember=False)
            return redirect(self.appbuilder.get_url_for_index)

        return handle_login()

    @expose("/logout/", methods=["GET", "POST"])
    def logout(self):
        oidc = self.appbuilder.sm.oid
        if auth_token := session.get("oidc_auth_token", ""):
            id_token = auth_token["id_token"]
            id_token_param = "&id_token_hint=" + quote(id_token)
        else:
            id_token_param = ""
        oidc.logout()
        super().logout()
        redirect_url = request.url_root.strip("/") + self.appbuilder.get_url_for_login
        return redirect(
            oidc.client_secrets.get("issuer")
            + "/protocol/openid-connect/logout?post_logout_redirect_uri="
            + quote(redirect_url)
            + id_token_param
        )
