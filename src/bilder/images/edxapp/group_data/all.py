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
        "git+https://github.com/openedx/super-csv.git@740b7c76cff72f0bbe6b1f3dde3a1086cf8e1cc3#egg=super-csv",
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
        "git+https://github.com/openedx/super-csv.git@740b7c76cff72f0bbe6b1f3dde3a1086cf8e1cc3#egg=super-csv",
    ],
    "mitx": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sga",
        "edx-sysadmin",
        "git+https://github.com/raccoongang/xblock-pdf.git@8d63047c53bc8fdd84fa7b0ec577bb0a729c215f#egg=xblock-pdf",
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "rapid-response-xblock==0.6.0",
        "ol-openedx-canvas-integration",
        "ol-openedx-rapid-response-reports",
        "git+https://github.com/openedx/super-csv.git@740b7c76cff72f0bbe6b1f3dde3a1086cf8e1cc3#egg=super-csv",
    ],
    "mitx-staging": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sga",
        "edx-sysadmin",
        "git+https://github.com/raccoongang/xblock-pdf.git@8d63047c53bc8fdd84fa7b0ec577bb0a729c215f#egg=xblock-pdf",
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "rapid-response-xblock",
        "ol-openedx-canvas-integration",
        "ol-openedx-rapid-response-reports",
        "git+https://github.com/openedx/super-csv.git@740b7c76cff72f0bbe6b1f3dde3a1086cf8e1cc3#egg=super-csv",
    ],
}

edx_plugins_removed = {
    "mitxonline": [
        "edx-name-affirmation",
    ],
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
        BRANCH: "mitx/nutmeg",
    },
    "mitx-staging": {
        "origin": "https://github.com/mitodl/edx-platform",
        BRANCH: "mitx/nutmeg",
    },
}
