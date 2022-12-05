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
        BRANCH: "nutmeg",
    },
    "mitx-staging": {
        REPOSITORY: "https://github.com/mitodl/mitx-theme",
        BRANCH: "nutmeg",
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
        "social-auth-mitxpro==0.6.1",
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
        "social-auth-mitxpro==0.6.1",
        "git+https://github.com/ubc/ubcpi.git@1.0.0#egg=ubcpi-xblock",
    ],
    "mitx": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sga",
        "edx-sysadmin",
        "git+https://github.com/raccoongang/xblock-pdf.git@8d63047c53bc8fdd84fa7b0ec577bb0a729c215f#egg=xblock-pdf",  # noqa: E501
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "rapid-response-xblock==0.6.0",
        "ol-openedx-canvas-integration",
        "ol-openedx-rapid-response-reports",
        # TODO: Identify source of failures in SAML integration when using lxml==4.9.1
        # Relevant errors message looks like: [2022-12-01 21:03:09 +0000] [79762] [INFO]
        # POST /auth/complete/tpa-saml/
        # func=xmlSecOpenSSLX509StoreVerify:file=x509vfy.c:
        # line=350:obj=x509-store:subj=unknown:error=71:certificate verification
        # failed:X509_verify_cert:
        # subject=/C=US/ST=Massachusetts/L=Cambridge/O=Massachusetts Institute of
        # Technology/OU=Open
        # Learning/CN=lms-ci.mitx.mit.edu/emailAddress=mitx-devops@mit.edu;
        # issuer=/C=US/ST=Massachusetts/L=Cambridge/O=Massachusetts Institute of
        # Technology/OU=Open
        # Learning/CN=lms-ci.mitx.mit.edu/emailAddress=mitx-devops@mit.edu; err=18;
        # msg=self signed certificate (TMM 2022-12-01)
        # Downgrade to version used in Nutmeg due to conflicts with SAML implementation
        "lxml==4.5.0",
    ],
    "mitx-staging": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sga",
        "edx-sysadmin",
        "git+https://github.com/raccoongang/xblock-pdf.git@8d63047c53bc8fdd84fa7b0ec577bb0a729c215f#egg=xblock-pdf",  # noqa: E501
        "ol-openedx-logging",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "rapid-response-xblock",
        "ol-openedx-canvas-integration",
        "ol-openedx-rapid-response-reports",
        # TODO: Identify source of failures in SAML integration when using lxml==4.9.1
        # Relevant errors message looks like: [2022-12-01 21:03:09 +0000] [79762] [INFO]
        # POST /auth/complete/tpa-saml/
        # func=xmlSecOpenSSLX509StoreVerify:file=x509vfy.c:
        # line=350:obj=x509-store:subj=unknown:error=71:certificate verification
        # failed:X509_verify_cert:
        # subject=/C=US/ST=Massachusetts/L=Cambridge/O=Massachusetts Institute of
        # Technology/OU=Open
        # Learning/CN=lms-ci.mitx.mit.edu/emailAddress=mitx-devops@mit.edu;
        # issuer=/C=US/ST=Massachusetts/L=Cambridge/O=Massachusetts Institute of
        # Technology/OU=Open
        # Learning/CN=lms-ci.mitx.mit.edu/emailAddress=mitx-devops@mit.edu; err=18;
        # msg=self signed certificate (TMM 2022-12-01)
        # Downgrade to version used in Nutmeg due to conflicts with SAML implementation
        "lxml==4.5.0",
    ],
}

edx_plugins_removed = {
    "mitxonline": [
        "edx-name-affirmation",
    ],
    "xpro": [],
    "mitx": [],
    "mitx-staging": [],
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
