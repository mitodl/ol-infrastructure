"""Per-environment configuration for the learn-ai static frontend deploy.

These values recreate the GitHub Actions ``vars.*`` that fed the three deploy
workflows deleted in https://github.com/mitodl/learn-ai/pull/13
(``ci-deploy.yml``, ``rc-deploy.yml``, ``production-deploy.yml``).

``build_vars`` mirrors two sources in the ``learn_ai`` Pulumi project:

* Five of the nine values are the ``learn_ai:frontend_vars`` block in
  ``src/ol_infrastructure/applications/learn_ai/Pulumi.{CI,QA,Production}.yaml``,
  prefixed with ``NEXT_PUBLIC_``.  That config block is no longer read by
  ``__main__.py`` -- it fed ``github.ActionsVariable`` resources that were
  removed in ``98e552bac``.
* The remaining four were derived in Pulumi code from
  ``learn_ai:learn_backend_domain`` before that code was removed.

Nothing keeps the two in sync automatically.  When a URL changes in the Pulumi
stack config, change it here too.

``bucket`` and ``fastly_domain`` come from ``learn_ai/__main__.py`` --
the bucket name is ``ol-mit-learn-ai-{env_suffix}``, and the domain comes from
``learn_ai:frontend_domain``.
under a ``/frontend/`` key prefix (``files/frontend_path_prefix.vcl``), so the
build output is synced to ``s3://<bucket>/frontend/`` rather than the root.
"""

from dataclasses import dataclass


@dataclass
class LearnAIFrontendEnv:
    """One deploy target for the learn-ai frontend."""

    stage: str  # "CI" | "QA" | "Production"
    branch: str  # branch of mitodl/learn-ai that feeds this environment
    bucket: str  # S3 bucket created by the learn_ai Pulumi stack
    fastly_domain: str  # hostname served by the environment's Fastly service
    build_vars: dict[str, str]  # NEXT_PUBLIC_* values baked into the export

    @property
    def slug(self) -> str:
        """Lowercased stage name, safe for use inside Concourse identifiers."""
        return self.stage.lower()


def _build_vars(
    learn_backend_domain: str,
    mit_learn_app_base_url: str,
    openedx_base_url: str,
) -> dict[str, str]:
    """Assemble the nine build-time vars for one environment.

    The MIT Search URLs point at production for every environment -- that is
    what ``learn_ai:frontend_vars`` specifies in all three stack configs, not an
    oversight here.
    """
    return {
        # learn-ai backend, served under /ai on the mit-learn API host
        "NEXT_PUBLIC_MITOL_API_BASE_URL": f"https://{learn_backend_domain}/ai",
        "NEXT_PUBLIC_AI_CSRF_COOKIE_NAME": "csrftoken",
        "NEXT_PUBLIC_MIT_LEARN_AI_LOGIN_URL": (
            f"https://{learn_backend_domain}/ai/http/login/"
        ),
        # mit-learn
        "NEXT_PUBLIC_MIT_LEARN_API_BASE_URL": f"https://{learn_backend_domain}/learn/",
        "NEXT_PUBLIC_MIT_LEARN_APP_BASE_URL": mit_learn_app_base_url,
        "NEXT_PUBLIC_MIT_SEARCH_ELASTIC_URL": (
            "https://api.learn.mit.edu/api/v1/learning_resources_search/"
        ),
        "NEXT_PUBLIC_MIT_SEARCH_VECTOR_URL": (
            "https://api.learn.mit.edu/api/v0/vector_learning_resources_search/"
        ),
        # openedx
        "NEXT_PUBLIC_OPENEDX_API_BASE_URL": openedx_base_url,
        "NEXT_PUBLIC_OPENEDX_LOGIN_URL": (
            f"{openedx_base_url}auth/login/ol-oauth2/?auth_entry=login"
        ),
    }


ENVIRONMENTS: list[LearnAIFrontendEnv] = [
    LearnAIFrontendEnv(
        stage="CI",
        branch="main",
        bucket="ol-mit-learn-ai-ci",
        fastly_domain="learn-ai-ci.ol.mit.edu",
        build_vars=_build_vars(
            learn_backend_domain="api.ci.learn.mit.edu",
            mit_learn_app_base_url="https://ci.learn.mit.edu/",
            openedx_base_url="https://courses.ci.learn.mit.edu/",
        ),
    ),
    LearnAIFrontendEnv(
        stage="QA",
        branch="release-candidate",
        bucket="ol-mit-learn-ai-qa",
        fastly_domain="learn-ai-qa.ol.mit.edu",
        build_vars=_build_vars(
            learn_backend_domain="api.rc.learn.mit.edu",
            mit_learn_app_base_url="https://rc.learn.mit.edu/",
            openedx_base_url="https://courses.rc.learn.mit.edu/",
        ),
    ),
    LearnAIFrontendEnv(
        stage="Production",
        branch="release",
        bucket="ol-mit-learn-ai-production",
        fastly_domain="learn-ai.ol.mit.edu",
        build_vars=_build_vars(
            learn_backend_domain="api.learn.mit.edu",
            mit_learn_app_base_url="https://learn.mit.edu/",
            openedx_base_url="https://courses.learn.mit.edu/",
        ),
    ),
]
