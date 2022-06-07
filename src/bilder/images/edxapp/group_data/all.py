REPOSITORY = "repository"
BRANCH = "branch"

edx_themes = {
    "mitxonline": {
        REPOSITORY: "https://github.com/mitodl/mitxonline-theme",
        BRANCH: "main",
    },
    "xpro": {
        REPOSITORY: "https://github.com/mitodl/mitxpro-theme",
        BRANCH: "maple",
    },
    "mitx": {
        REPOSITORY: "https://github.com/mitodl/mitx-theme",
        BRANCH: "maple",
    },
    "mitx-staging": {
        REPOSITORY: "https://github.com/mitodl/mitx-theme",
        BRANCH: "maple",
    },
}

# TODO: Remove references to super-csv once this PR is merged:
# https://github.com/openedx/super-csv/pull/100 (TMM 2022-06-07)
edx_plugins_added = {
    "mitxonline": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sysadmin",
        "edx-username-changer==0.3.0",
        "mitxpro-openedx-extensions==1.0.0",
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "ol-openedx-checkout-external",
        "social-auth-mitxpro==0.6",
        "git+https://github.com/arslanashraf7/super-csv@740b7c76cff72f0bbe6b1f3dde3a1086cf8e1cc3#egg=super-csv",  # noqa: E501
    ],
    "xpro": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sysadmin",
        "edx-username-changer==0.3.0",
        "mitxpro-openedx-extensions==1.0.0",
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "social-auth-mitxpro==0.6",
        "git+https://github.com/ubc/ubcpi.git@1.0.0#egg=ubcpi-xblock",
        "git+https://github.com/arslanashraf7/super-csv@740b7c76cff72f0bbe6b1f3dde3a1086cf8e1cc3#egg=super-csv",  # noqa: E501
    ],
    "mitx": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sga==0.17.3",  # remove pin when upgrading to nutmeg
        "edx-sysadmin",
        "git+https://github.com/raccoongang/xblock-pdf.git@8d63047c53bc8fdd84fa7b0ec577bb0a729c215f#egg=xblock-pdf",  # noqa: E501
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "rapid-response-xblock==0.6.0",
        "ol-openedx-canvas-integration==0.1.1",  # TODO: Remove pin when upgrading to nutmeg
        "ol-openedx-rapid-response-reports==0.1.0",  # TODO: Remove pin when upgrading to nutmeg
        "git+https://github.com/arslanashraf7/super-csv@740b7c76cff72f0bbe6b1f3dde3a1086cf8e1cc3#egg=super-csv",  # noqa: E501
    ],
    "mitx-staging": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sga==0.17.3",  # remove pin when upgrading to nutmeg
        "edx-sysadmin",
        "git+https://github.com/raccoongang/xblock-pdf.git@8d63047c53bc8fdd84fa7b0ec577bb0a729c215f#egg=xblock-pdf",
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "rapid-response-xblock==0.6.0",
        "ol-openedx-canvas-integration==0.1.1",  # TODO: Remove pin when upgrading to nutmeg
        "ol-openedx-rapid-response-reports==0.1.0",  # TODO:  Remove pin when upgrading to nutmeg
        "git+https://github.com/arslanashraf7/super-csv@740b7c76cff72f0bbe6b1f3dde3a1086cf8e1cc3#egg=super-csv",  # noqa: E501
    ],
}

edx_plugins_removed = {
    "mitxonline": [
        "edx-name-affirmation",
        "super-csv",
    ],
    "xpro": ["super-csv"],
    "mitx": ["super-csv"],
    "mitx-staging": ["super-csv"],
}

edx_platform_repository = {
    "mitxonline": {
        "origin": "https://github.com/openedx/edx-platform",
        BRANCH: "release",
    },
    "xpro": {
        "origin": "https://github.com/openedx/edx-platform",
        BRANCH: "open-release/maple.master",
    },
    "mitx": {
        "origin": "https://github.com/mitodl/edx-platform",
        BRANCH: "mitx/maple",
    },
    "mitx-staging": {
        "origin": "https://github.com/mitodl/edx-platform",
        BRANCH: "mitx/maple",
    },
}
