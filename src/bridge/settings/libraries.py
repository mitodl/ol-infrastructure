"""Canonical registry of publishable libraries: pipeline, Concourse team, publish job.

The sibling of ``bridge.settings.apps``. Apps are *released* (cut a version,
deploy an image to RC, promote to production); libraries are *published* (build
an artifact and upload it to PyPI or npm). They share almost nothing: a library
has no RC/Production distinction, no release issue, no checklist gate, and no
Kubernetes deployment. So rather than widen ``AppRegistration`` with fields
that mean nothing for an app, libraries get their own registry, which the
release bot loads alongside ``APPS``.

Consumed by the release bot's Pulumi stack
(``src/ol_infrastructure/applications/release_bot/__main__.py``), which renders
it into the ``LIBRARIES_CONFIG`` env var that ``/doof publish`` reads.

EVERY FACT BELOW WAS READ OFF THE LIVE CONCOURSE, not off the generators.
Pipeline names, the owning team, and the job names were confirmed against
``https://cicd.odl.mit.edu/api/v1/teams/main/pipelines`` on 2026-09-04. That
matters because the generators do not state their team anywhere -- it is only
implied by the ``fly -t pr-main`` line each one prints as usage, versus
``fly -t pr-inf`` for the app pipelines.

THE TEAM IS THE REASON ``/doof publish`` COULD NEVER HAVE WORKED. The bot runs
with ``CONCOURSE_TEAM=infrastructure`` (set in its Pulumi stack), which is where
the ``<app>-pipeline`` pipelines live. Every library publish pipeline is in team
``main``. Even a correct pipeline and job name would have 404'd, so the team is
carried per-library here rather than taken from the bot's ambient default.
"""

from dataclasses import dataclass

#: Concourse team that owns every library publish pipeline. Distinct from the
#: release bot's own ``CONCOURSE_TEAM`` (``infrastructure``), which owns the
#: per-app release pipelines.
PUBLISH_TEAM = "main"


@dataclass(frozen=True)
class LibraryRegistration:
    """Canonical metadata for one publishable library.

    :param pipeline: Concourse pipeline that publishes it.
    :param team: Concourse team owning ``pipeline``.
    :param publish_job: The single job that publishes. Mutually exclusive with
        ``package_job_prefix``.
    :param package_job_prefix: Set for a monorepo, whose pipeline carries one
        publish job per package: the job for package ``X`` is
        ``f"{package_job_prefix}{X}"``. The package list is deliberately NOT
        recorded here -- it is discovered from the pipeline's live job list, so
        a package added to the monorepo becomes publishable as soon as the
        pipeline is regenerated, with no edit to this file.
    :param github_repo: "owner/repo" slug, for the human reading the reply.
    :param registry: Where the artifact lands, for the same reason.
    """

    pipeline: str
    team: str = PUBLISH_TEAM
    publish_job: str | None = None
    package_job_prefix: str | None = None
    github_repo: str | None = None
    registry: str = "PyPI"

    def __post_init__(self) -> None:
        if bool(self.publish_job) == bool(self.package_job_prefix):
            msg = (
                f"{self.pipeline}: set exactly one of publish_job "
                "(single-artifact) or package_job_prefix (monorepo)."
            )
            raise ValueError(msg)


LIBRARIES: dict[str, LibraryRegistration] = {
    # Monorepos: `/doof publish <library> <package>` -> job build-<package>.
    "ol-django": LibraryRegistration(
        pipeline="publish-ol-django-pypi",
        package_job_prefix="build-",
        github_repo="mitodl/ol-django",
    ),
    "open-edx-plugins": LibraryRegistration(
        pipeline="publish-open-edx-plugins-pypi",
        package_job_prefix="build-",
        github_repo="mitodl/open-edx-plugins",
    ),
    # The pipeline generator lives in ol-infrastructure; the packages it
    # publishes do not.
    "jupyterhub-extensions": LibraryRegistration(
        pipeline="publish-jupyterhub-extensions-pypi",
        package_job_prefix="build-",
        github_repo="mitodl/ol-notebook-extensions",
    ),
    # Single-artifact.
    "ol-concourse": LibraryRegistration(
        pipeline="publish-ol-concourse",
        publish_job="publish-ol-concourse-lib",
        github_repo="mitodl/ol-concourse",
    ),
    # The npm API clients. Their `publish` job declares
    # `passed: [generate-clients]`, which constrains which *version* it can
    # publish, not whether it can be triggered by hand -- so a manual publish
    # ships whatever generate-clients last produced, never something newer.
    "mit-learn-api-client": LibraryRegistration(
        pipeline="mit-learn-api-client",
        publish_job="publish",
        github_repo="mitodl/mit-learn-api-clients",
        registry="npm",
    ),
    "mitxonline-api-client": LibraryRegistration(
        pipeline="mitxonline-api-client",
        publish_job="publish",
        github_repo="mitodl/mitxonline-api-clients",
        registry="npm",
    ),
}


#: Libraries Doof's `@doof publish <version>` could publish (project_type
#: "library" in mitodl/release-script repos_info.json) that have NO Concourse
#: publish pipeline. Mapped to why, so `/doof publish <name>` can answer
#: instead of saying "unknown". Each disposition was checked against the live
#: GitHub repo on 2026-09-04.
#:
#: Only two of Doof's eight are a real gap: edx-sga and edx-api-client. Three
#: repos are archived, one moved to GitHub Actions, one has been dormant since
#: 2018, and one was never publishable through Doof in the first place.
UNPUBLISHABLE_LIBRARIES: dict[str, str] = {
    "edx-sga": (
        "no publish pipeline yet — the repo is active (last push 2026-07-29) "
        "and its only workflow is CI, so PyPI releases still go through Doof."
    ),
    "edx-api-client": (
        "no publish pipeline yet — the repo is active (last push 2026-08-08) "
        "and its only workflows are CI and static analysis, so PyPI releases "
        "still go through Doof."
    ),
    "rapid-response-xblock": (
        "superseded — the repo is archived and the package is now built by "
        "`build-rapid_response_xblock` in publish-open-edx-plugins-pypi. "
        "Publish it with `/doof publish open-edx-plugins rapid_response_xblock`."
    ),
    "open-discussions-client": "retired — the repo is archived.",
    "mit-open-login-button": "retired — the repo is archived.",
    "course-search-utils": (
        "not published from Concourse by design — the repo releases itself via "
        "its GitHub Actions semantic-release workflow using npm trusted "
        "publishing (OIDC)."
    ),
    "mit-moira": "dormant — no commits since 2018 and no CI workflows.",
    "ocw-hugo-projects": (
        "not a published package — Doof recorded it with packaging_tool "
        '"none", so `@doof publish` raised on it too.'
    ),
}
