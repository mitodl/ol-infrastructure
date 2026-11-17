# A release that reached Production. ol-analytics-api and mitxonline cut
# releases as bare CalVer tags (2026.9.17.1) through the release-resource
# workflow and keep no long-lived release branch, and their Production jobs add
# `production/<version>` (bridge.settings.apps.PRODUCTION_TAG_PREFIX) at the
# release tag once the release is live. Watching those rather than the release
# tags publishes a client only for a promoted release: a published,
# non-prerelease client means "ready to use", and an abandoned release
# publishes nothing.
#
# POSIX ERE, not PCRE: the resource filters tags with `grep -E`, where `\d` is
# not a digit class. GNU grep warns "stray \ before d" and drops it, leaving a
# pattern that matches nothing and a pipeline that never fires on a release.
# The anchors exclude the bare release tags, the legacy vX.Y.Z tags and the
# release resource's abandoned/<version> tombstones.
PRODUCTION_RELEASE_TAG_REGEX = r"^production/[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}\.[0-9]+$"

PIPELINE_CONFIGS = {
    "ol-analytics-api": {
        "source_repo_name": "ol-analytics-api",
        "source_repo_uri": "https://github.com/mitodl/ol-analytics-api",
        "source_repo_branch": "main",
        "source_repo_tag_regex": PRODUCTION_RELEASE_TAG_REGEX,
        "client_repo_name": "ol-analytics-api-clients",
        "client_repo_uri": "git@github.com:mitodl/ol-analytics-api-clients.git",
        "client_repo_branch": "main",
        # One package per tenant: the two are independent APIs with different
        # auth and audiences (an org-manager dashboard vs a machine-to-machine
        # partner integration), and a consumer of one has no reason to pull the
        # other's types in. Each name must match a directory under
        # src/typescript/ in the client repo.
        "client_repo_subpath": [
            "ol-analytics-dashboard-api-axios",
            "ol-analytics-learner-records-api-axios",
        ],
    },
    # mit-learn is still on the legacy flow and watches its `release` branch,
    # which Doof merges a release candidate into only when it is promoted.
    # Move it to PRODUCTION_RELEASE_TAG_REGEX, and so to versions taken from
    # its releases rather than bumpver, when its app flips to
    # `release_resource_workflow` in bridge/settings/apps.py.
    "mit-learn": {
        "source_repo_name": "mit-learn",
        "source_repo_uri": "https://github.com/mitodl/mit-learn",
        "source_repo_branch": "release",
        "client_repo_name": "mit-learn-api-clients",
        "client_repo_uri": "git@github.com:mitodl/mit-learn-api-clients",
        "client_repo_branch": "main",
        "client_repo_subpath": "mit-learn-api-axios",
        # A frontend branch can pin a `next` prerelease for development, review
        # and CI as soon as the backend change it needs merges to main. It
        # still merges only on a stable client: mit-learn CI refuses `next`.
        "prerelease_branch": "main",
    },
    "mitxonline": {
        "source_repo_name": "mitxonline",
        "source_repo_uri": "https://github.com/mitodl/mitxonline",
        "source_repo_branch": "main",
        "source_repo_tag_regex": PRODUCTION_RELEASE_TAG_REGEX,
        "client_repo_name": "mitxonline-api-clients",
        "client_repo_uri": "git@github.com:mitodl/mitxonline-api-clients.git",
        "client_repo_branch": "main",
        "client_repo_subpath": "mitxonline-api-axios",
    },
}
