import uuid

from jupyterhub.auth import Authenticator
from jupyterhub.handlers import BaseHandler
from jupyterhub.utils import url_path_join
from traitlets import Unicode, default


class TmpAuthenticateHandler(BaseHandler):
    """
    Provides a GET web request handler for /hub/tmplogin, as registered by
    TmpAuthenticator's override of Authenticator.get_handlers.

    JupyterHub will redirect here if it doesn't recognize a user via a cookie,
    but users can also visit /hub/tmplogin explicitly to get setup with a new
    user.
    """

    async def get(self):
        """
        Authenticate as a new random user no matter what.

        This GET request handler mimics parts of what's done by JupyterHub's
        LoginHandler when a user isn't recognized: to first call
        BaseHandler.login_user and then redirect the user onwards. The
        difference is that here users always login as a new user.

        By overwriting any previous user's identifying cookie, it acts as a
        combination of a logout and login handler.

        JupyterHub's LoginHandler ref: https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/handlers/login.py#L129-L138
        """
        # Login as a new user, without checking if we were already logged in
        #
        user = await self.login_user(None)

        # Set or overwrite the login cookie to recognize the new user.
        #
        # login_user calls set_login_cookie(user), that sets a login cookie for
        # the user via set_hub_cookie(user), but only if it doesn't recognize a
        # user from an pre-existing login cookie. Due to that, we
        # unconditionally call self.set_hub_cookie(user) here.
        #
        # BaseHandler.login_user:                   https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/handlers/base.py#L823-L843
        # - BaseHandler.authenticate:               https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/handlers/base.py#L643-L644
        #   - Authenticator.get_authenticated_user: https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/auth.py#L472-L534
        # - BaseHandler.auth_to_user:               https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/handlers/base.py#L774-L821
        # - BaseHandler.set_login_cookie:           https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/handlers/base.py#L627-L628
        #   - BaseHandler.set_session_cookie:       https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/handlers/base.py#L601-L613
        #   - BaseHandler.set_hub_cookie:           https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/handlers/base.py#L623-L625
        #
        self.set_hub_cookie(user)

        # Login complete, redirect the user.
        #
        # BaseHandler.get_next_url ref: https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/handlers/base.py#L646-L653
        #
        next_url = self.get_next_url(user)
        self.redirect(next_url)


class TmpAuthenticator(Authenticator):
    """
    When JupyterHub is configured to use this authenticator, visiting the home
    page immediately logs the user in with a randomly generated UUID if they are
    already not logged in, and spawns a server for them.
    """

    @default("auto_login")
    def _auto_login_default(self):
        """
        The Authenticator base class' config auto_login defaults to False, but
        we change that default to True in TmpAuthenticator. This makes users
        automatically get logged in when they hit the hub's home page, without
        requiring them to click a 'login' button.

        JupyterHub admins can still opt back to present the /hub/login page with
        the login button like this:

            c.TmpAuthenticator.auto_login = False
        """
        return True

    login_service = Unicode(
        "Automatic Temporary Credentials",
        help="""
        Text to be shown with the 'Sign in with ...' button, when auto_login is
        False.

        The Authenticator base class' login_service isn't tagged as a
        configurable traitlet, so we redefine it to allow it to be configurable
        like this:

            c.TmpAuthenticator.login_service = "your inherent worth as a human being"
        """,
    ).tag(config=True)

    async def authenticate(self, handler, data):
        """
        Always authenticate a new user by generating a universally unique
        identifier (uuid).
        """
        username = str(uuid.uuid4())
        return {
            "name": username,
        }

    def get_handlers(self, app):
        """
        Registers a dedicated endpoint and web request handler for logging in
        with TmpAuthenticator. This is needed as /hub/login is reserved for
        redirecting to what's returned by login_url.

        ref: https://github.com/jupyterhub/jupyterhub/pull/1066
        """
        return [("/tmplogin", TmpAuthenticateHandler)]

    def login_url(self, base_url):
        """
        login_url is overridden as intended for Authenticator subclasses that
        provides a custom login handler (for /hub/tmplogin).

        JupyterHub redirects users to this destination from /hub/login if
        auto_login is set, or if its not set and users press the "Sign in ..."
        button.

        ref: https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/auth.py#L708-L723
        ref: https://github.com/jupyterhub/jupyterhub/blob/4.0.0/jupyterhub/handlers/login.py#L118-L147
        """
        return url_path_join(base_url, "tmplogin")


c.JupyterHub.authenticator_class = TmpAuthenticator
c.Authenticator.allow_all = True

from kubespawner import KubeSpawner


class QueryStringKubeSpawner(KubeSpawner):
    def start(self):
        image_base = (
            "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks:{}"
        )
        KNOWN_COURSES = [
            "clustering_and_descriptive_ai",
            "deep_learning_foundations_and_applications",
            "supervised_learning_fundamentals",
            "introduction_to_data_analytics_and_machine_learning",
        ]
        self.image = "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks:clustering_and_descriptive_ai"
        if self.handler:
            course = self.handler.get_query_argument("course", "").lower()
            if course in KNOWN_COURSES:
                self.image = image_base.format(course)
        return super().start()


c.JupyterHub.spawner_class = QueryStringKubeSpawner
