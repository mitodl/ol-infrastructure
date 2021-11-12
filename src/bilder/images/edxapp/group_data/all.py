edx_themes = [
    {
        "mitxonline": {
            "repository": "https://github.com/mitodl/mitxonline-theme",
            "branch": "main",
        },
        "xpro": {
            "repository": "https://github.com/mitodl/mitxpro-theme",
            "branch": "maple",
        },
        "mitx": {
            "repository": "https://github.com/mitodl/mitx-theme",
            "branch": "maple",
        },
    }
]

edx_plugins = {
    "mitxonline": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sysadmin",
        "edx-username-changer==0.2.0",
        "mitxpro-openedx-extensions==0.2.2",
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "social-auth-mitxpro==0.5",
    ],
    "xpro": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sysadmin",
        "edx-username-changer==0.2.0",
        "mitxpro-openedx-extensions==0.2.2",
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "social-auth-mitxpro==0.5",
    ],
    "mitx": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sga==0.17.2",
        "edx-sysadmin",
        "git+https://github.com/raccoongang/xblock-pdf.git@8d63047c53bc8fdd84fa7b0ec577bb0a729c215f#egg=xblock-pdf",
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "rapid-response-xblock==0.1.0",
    ],
}
