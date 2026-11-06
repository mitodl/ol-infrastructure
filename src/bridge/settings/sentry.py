"""Sentry identifiers that pipeline code needs at generation time.

The Sentry organization and its projects are managed by the Pulumi program in
``src/ol_infrastructure/infrastructure/sentry/__main__.py``. That program is
regenerated from the live organization by ``bin/import-sentry-config``, so
nothing can import a constant from it, and it cannot import one from here
without the next regeneration dropping the import. The slugs below are
therefore a second copy of what that program declares, and
``tests/ol_concourse/test_k8s_apps_sourcemaps.py`` checks the two agree.

A pipeline that passes a stale project slug to ``sentry-cli`` fails with
``One or more projects are invalid``, and only after a release image is
pushed. The test catches a rename at CI time instead. Keep the slug here,
where that test checks it, rather than inline in a pipeline.
"""

#: Slug of the Sentry organization, as ``sentry-cli --org`` wants it.
SENTRY_ORG_SLUG = "mit-office-of-digital-learning"

#: Slug of the Sentry project that both the MIT Learn Django backend and the
#: Next.js frontend report to.
MIT_LEARN_SENTRY_PROJECT_SLUG = "mit-learn"
