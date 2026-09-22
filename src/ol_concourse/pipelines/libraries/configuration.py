PIPELINE_CONFIGS = {
    # ol-analytics-api cuts releases as bare CalVer tags (2026.9.17.1) and keeps
    # no long-lived release branch, so this one versions on tags. Everything
    # else here watches a `release` branch.
    "ol-analytics-api": {
        "source_repo_name": "ol-analytics-api",
        "source_repo_uri": "https://github.com/mitodl/ol-analytics-api",
        "source_repo_branch": "main",
        "source_repo_tag_regex": r"^\d{4}\.\d{1,2}\.\d{1,2}\.\d+$",
        "client_repo_name": "ol-analytics-api-clients",
        "client_repo_uri": "git@github.com:mitodl/ol-analytics-api-clients.git",
        "client_repo_branch": "main",
        "client_repo_subpath": "ol-analytics-api-axios",
    },
    "mit-learn": {
        "source_repo_name": "mit-learn",
        "source_repo_uri": "https://github.com/mitodl/mit-learn",
        "source_repo_branch": "release",
        "client_repo_name": "mit-learn-api-clients",
        "client_repo_uri": "git@github.com:mitodl/mit-learn-api-clients",
        "client_repo_branch": "main",
        "client_repo_subpath": "mit-learn-api-axios",
    },
    "mitxonline": {
        "source_repo_name": "mitxonline",
        "source_repo_uri": "https://github.com/mitodl/mitxonline",
        "source_repo_branch": "release",
        "client_repo_name": "mitxonline-api-clients",
        "client_repo_uri": "git@github.com:mitodl/mitxonline-api-clients.git",
        "client_repo_branch": "main",
        "client_repo_subpath": "mitxonline-api-axios",
    },
}
