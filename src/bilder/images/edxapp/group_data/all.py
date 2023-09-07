REPOSITORY = "repository"
BRANCH = "branch"

edx_plugins_added = {
    "mitxonline": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-proctoring-proctortrack==1.1.1",
        "edx-sysadmin",
        "edx-username-changer==0.3.0",
        "mitxpro-openedx-extensions==1.0.0",
        "ol-openedx-logging==0.1.0",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "ol-openedx-checkout-external",
        "sentry-sdk==1.21.1",  # Fix RecursionError
        "social-auth-mitxpro==0.6.2",
    ],
    "xpro": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sysadmin",
        "edx-username-changer==0.3.0",
        "mitxpro-openedx-extensions==1.0.0",
        "ol-openedx-logging==0.1.0",
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
        "ol-openedx-logging==0.1.0",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "rapid-response-xblock==0.6.0",
        "ol-openedx-canvas-integration",
        "ol-openedx-rapid-response-reports",
        # TODO: Identify source of failures in SAML integration when using lxml==4.9.1  # noqa: E501, FIX002, TD002, TD003
        # Relevant errors message looks like: [2022-12-01 21:03:09 +0000] [79762] [INFO]
        # POST /auth/complete/tpa-saml/
        # func=xmlSecOpenSSLX509StoreVerify:file=x509vfy.c:
        # line=350:obj=x509-store:subj=unknown:error=71:certificate verification
        # failed:X509_verify_cert:
        # subject=/C=US/ST=Massachusetts/L=Cambridge/O=Massachusetts Institute of
        # issuer=/C=US/ST=Massachusetts/L=Cambridge/O=Massachusetts Institute of
        # msg=self signed certificate (TMM 2022-12-01)
        # Downgrade to version used in Nutmeg due to conflicts with SAML implementation
        "lxml==4.5.0",
        "sentry-sdk==1.21.1",  # Fix RecursionError
    ],
    "mitx-staging": [
        "celery-redbeat",  # Support for using Redis as the lock for Celery schedules
        "django-redis",  # Support for Redis caching in Django
        "edx-sga",
        "edx-sysadmin",
        "git+https://github.com/raccoongang/xblock-pdf.git@8d63047c53bc8fdd84fa7b0ec577bb0a729c215f#egg=xblock-pdf",  # noqa: E501
        "ol-openedx-logging==0.1.0",
        "ol-openedx-sentry",
        "ol-openedx-course-export",
        "rapid-response-xblock",
        "ol-openedx-canvas-integration",
        "ol-openedx-rapid-response-reports",
        # TODO: Identify source of failures in SAML integration when using lxml==4.9.1  # noqa: E501, FIX002, TD002, TD003
        # Relevant errors message looks like: [2022-12-01 21:03:09 +0000] [79762] [INFO]
        # POST /auth/complete/tpa-saml/
        # func=xmlSecOpenSSLX509StoreVerify:file=x509vfy.c:
        # line=350:obj=x509-store:subj=unknown:error=71:certificate verification
        # failed:X509_verify_cert:
        # subject=/C=US/ST=Massachusetts/L=Cambridge/O=Massachusetts Institute of
        # issuer=/C=US/ST=Massachusetts/L=Cambridge/O=Massachusetts Institute of
        # msg=self signed certificate (TMM 2022-12-01)
        # Downgrade to version used in Nutmeg due to conflicts with SAML implementation
        "lxml==4.5.0",
        "sentry-sdk==1.21.1",  # Fix RecursionError
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
