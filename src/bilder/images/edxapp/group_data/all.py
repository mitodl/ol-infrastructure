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

edx_plugins = {
    "mitxonline": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sysadmin",
        "edx-username-changer==0.3.0",
        "mitxpro-openedx-extensions==1.0.0",
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "social-auth-mitxpro==0.6",
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
        "ol-openedx-canvas-integration",
        "ol-openedx-rapid-response-reports",
    ],
    "mitx-staging": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sga==0.17.3",  # remove pin when upgrading to nutmeg
        "edx-sysadmin",
        "git+https://github.com/raccoongang/xblock-pdf.git@8d63047c53bc8fdd84fa7b0ec577bb0a729c215f#egg=xblock-pdf",  # noqa: E501
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "rapid-response-xblock==0.6.0",
        "ol-openedx-canvas-integration",
        "ol-openedx-rapid-response-reports",
    ],
}

edx_platform_repository = {
    "mitxonline": {
        "origin": "https://github.com/edx/edx-platform",
        BRANCH: "release",
    },
    "xpro": {
        "origin": "https://github.com/mitodl/edx-platform",
        BRANCH: "xpro/maple",
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
