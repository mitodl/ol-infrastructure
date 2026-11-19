"""What the local mitxonline program reads before it builds bindings.

Kept apart from ``__main__.py`` so the steps appended to the deployed pre-deploy
command are plain functions with tests. The decisions every Tilt-run program
shares (which image runs, how the stack file's env map merges, what Pulumi waits
on and how the Job fails) are in ``ol_infrastructure.lib.local_dev_program``.
"""

IMAGE_REPOSITORY = "mitodl/mitxonline-app"
# The composed Open edX authenticates to mitxonline with the OAuth2 Application
# that mitxonline's openedx/migrations/0001_add_oauth_application creates, but
# only on a fresh database, with a random secret that django-oauth-toolkit
# hashes on save and cannot read back. Converging it with the LMS's
# OL_OAUTH2_CLIENT_{ID,SECRET} therefore means re-asserting the row from
# OPENEDX_OAUTH_APP_CLIENT_{ID,SECRET} in the Job's env whenever the Job runs.
# `configure_instance`, the documented way, exits before its OAuth step without
# a platform argument and builds redirect URIs from devstack hosts. Appended to
# the deployed migrate command, in the same shell, so it runs after the tables
# exist and a failure fails the Job. Quoted heredoc: the shell leaves the Python
# alone.
# The Wagtail home page, Site row and index pages a fresh database lacks. Every
# step is an ensure_* call, so it is safe on every Job run; the deployed stacks'
# databases never start empty, so the deployed pre-deploy command omits it.
CONFIGURE_WAGTAIL_STEP = " && python manage.py configure_wagtail"
OPENEDX_OAUTH_APP_SCRIPT = """import os
from urllib.parse import urljoin

from django.conf import settings
from oauth2_provider.models import get_application_model

redirect_uri = urljoin(
    settings.OPENEDX_BASE_REDIRECT_URL,
    f"/auth/complete/{settings.OPENEDX_OAUTH_PROVIDER}/",
)
_, created = get_application_model().objects.update_or_create(
    name=settings.OPENEDX_OAUTH_APP_NAME,
    defaults={
        "client_id": os.environ["OPENEDX_OAUTH_APP_CLIENT_ID"],
        "client_secret": os.environ["OPENEDX_OAUTH_APP_CLIENT_SECRET"],
        "client_type": "confidential",
        "authorization_grant_type": "authorization-code",
        "skip_authorization": True,
        "redirect_uris": redirect_uri,
        "user": None,
    },
)
verb = "created" if created else "updated"
print(f"{verb} {settings.OPENEDX_OAUTH_APP_NAME} -> {redirect_uri}")
"""


def manage_py_shell_step(*scripts: str) -> str:
    """Return a step that runs ``scripts`` in one ``manage.py shell``.

    One heredoc for all of them: bash ends a heredoc only at a line that is
    exactly its delimiter, so a second ``&& ... <<'PY'`` step after one would
    land inside the first's input instead of running.
    """
    return " && python manage.py shell <<'PY'\n" + "".join(scripts) + "PY"


OPENEDX_OAUTH_APP_STEP = manage_py_shell_step(OPENEDX_OAUTH_APP_SCRIPT)
# ol-analytics-api's org-manager check authenticates to mitxonline with a
# client-credentials Application (mitodl/mitxonline#3807), created by hand in
# the Django admin when deployed. Locally the Job re-asserts it while
# ol-analytics-api is enabled. local-dev/apps/ol-analytics-api/
# secrets.yaml holds the same pair, and tests/local_dev keeps the two equal.
ANALYTICS_API_CLIENT_ID = "ol-analytics-api-local"
ANALYTICS_API_CLIENT_SECRET = "local-dev-ol-analytics-api-mitxonline-secret"  # noqa: S105  # pragma: allowlist secret
ANALYTICS_API_OAUTH_APP_SCRIPT = f"""\
from oauth2_provider.models import get_application_model

Application = get_application_model()
_, created = Application.objects.update_or_create(
    client_id={ANALYTICS_API_CLIENT_ID!r},
    defaults=dict(
        name="ol-analytics-api (local-dev)",
        client_secret={ANALYTICS_API_CLIENT_SECRET!r},
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
    ),
)
print("created" if created else "updated", "ol-analytics-api (local-dev)")
"""


def with_local_steps(
    pre_deploy_commands: list[tuple[str, list[str]]] | None,
    *,
    analytics_api: bool,
) -> list[tuple[str, list[str]]]:
    """Append CONFIGURE_WAGTAIL_STEP and OPENEDX_OAUTH_APP_STEP to the first deployed
    pre-deploy command, in that order and in the same shell, so they run after
    migrate and never beside it. While ol-analytics-api is enabled,
    ANALYTICS_API_OAUTH_APP_SCRIPT runs in the same manage.py shell as the Open
    edX script.

    The component's field is optional; with no command to append to the steps
    would silently never run, so that is an error here.
    """
    if not pre_deploy_commands:
        msg = "the deployed definition has no pre_deploy_commands to append to"
        raise ValueError(msg)
    (name, command), *rest = pre_deploy_commands
    scripts = [OPENEDX_OAUTH_APP_SCRIPT]
    if analytics_api:
        scripts.append(ANALYTICS_API_OAUTH_APP_SCRIPT)
    local_steps = CONFIGURE_WAGTAIL_STEP + manage_py_shell_step(*scripts)
    return [(name, [*command[:-1], command[-1] + local_steps]), *rest]
