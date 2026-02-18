# ruff: noqa: E501
"""Configuration builder for EDXApp Kubernetes ConfigMaps.

This module provides a dictionary-based configuration system that replaces
nested f-strings and duplicate YAML files with maintainable, testable code.

The architecture supports per-deployment customization while maintaining a
single source of truth for shared configuration across all deployments.

Settings Structure (aligning with Open edX's three-tier architecture):
- Module-level settings: Individual feature flags extracted from FEATURES dict
- FEATURES dict: Remaining feature flags that don't have module-level equivalents
- Deployment overrides: Per-environment customizations

See: https://github.com/openedx/edx-platform/blob/master/openedx/envs/common.py
"""

from dataclasses import dataclass
from typing import Any, TypeAlias

import yaml

# Type alias for configuration dictionaries
ConfigDict: TypeAlias = dict[str, Any]  # noqa: UP040


@dataclass
class DeploymentConfig:
    """Per-deployment configuration override."""

    env_prefix: str
    """Environment prefix (mitx, xpro, mitxonline, mitx-staging)."""
    overrides: ConfigDict
    """Configuration values that override base settings."""


def deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    """Deep merge overrides into base configuration, preserving nested structures.

    Args:
        base: Base configuration dictionary
        overrides: Overrides to merge into base

    Returns:
        Merged configuration dictionary
    """
    result = base.copy()

    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Recursively merge nested dicts
            result[key] = deep_merge(result[key], value)
        else:
            # Override or add value
            result[key] = value

    return result


def build_base_general_config() -> ConfigDict:
    """Build the base 50-general-config.yaml configuration.

    This contains all settings shared across deployments. Deployment-specific
    overrides are applied via the deployment config dictionaries.

    Module-level settings (extracted from FEATURES dict) are now top-level keys,
    following the pattern in openedx/envs/common.py. The FEATURES dict contains
    only feature flags that don't have module-level equivalents.

    Returns:
        Base configuration dictionary
    """
    return {
        # MongoDB-related configs moved to secrets (02-mongodb-credentials-yaml):
        # - DOC_STORE_CONFIG
        # - CONTENTSTORE
        # These now include full MongoDB connection params in the secrets template
        "ACCOUNT_MICROFRONTEND_URL": None,
        "ACTIVATION_EMAIL_SUPPORT_LINK": "",
        "AFFILIATE_COOKIE_NAME": "dev_affiliate_id",
        "AUTH_PASSWORD_VALIDATORS": [
            {
                "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
            },
            {
                "NAME": "common.djangoapps.util.password_policy_validators.MinimumLengthValidator",
                "OPTIONS": {"min_length": 2},
            },
            {
                "NAME": "common.djangoapps.util.password_policy_validators.MaximumLengthValidator",
                "OPTIONS": {"max_length": 75},
            },
        ],
        "AWS_ACCESS_KEY_ID": "",
        "AWS_SECRET_ACCESS_KEY": "",
        "AWS_SES_REGION_ENDPOINT": "email.us-east-1.amazonaws.com",
        "AWS_SES_REGION_NAME": "us-east-1",
        "BLOCKSTORE_USE_BLOCKSTORE_APP_API": True,
        "BUNDLE_ASSET_STORAGE_SETTINGS": {
            "STORAGE_CLASS": "storages.backends.s3boto3.S3Boto3Storage",
            "STORAGE_KWARGS": {
                "location": "blockstore/",
            },
        },
        "BRANCH_IO_KEY": "",
        "BUGS_EMAIL": "odl-devops@mit.edu",
        "BULK_EMAIL_EMAILS_PER_TASK": 500,
        "BULK_EMAIL_LOG_SENT_EMAILS": False,
        "CELERYBEAT_SCHEDULER": "redbeat.RedBeatScheduler",
        "CELERY_BROKER_TRANSPORT": "rediss",
        "CELERY_BROKER_USER": "default",
        "CELERY_BROKER_USE_SSL": {
            "ssl_cert_reqs": "optional",
        },
        "CELERY_BROKER_VHOST": "1",
        "CELERY_EVENT_QUEUE_TTL": None,
        "CELERY_TIMEZONE": "UTC",
        "CELERY_TASK_TRACK_STARTED": True,
        "CELERY_TASK_SEND_SENT_EVENT": True,
        "CERTIFICATE_TEMPLATE_LANGUAGES": {
            "en": "English",
            "es": "Español",
        },
        "CERT_QUEUE": "certificates",
        "CONTACT_MAILING_ADDRESS": "SET-ME-PLEASE",
        "CONTACT_US_ENABLE": False,
        "ENABLE_CODEJAIL_REST_SERVICE": True,
        "CODE_JAIL_REST_SERVICE_HOST": "http://codejail:8000",
        "COMPREHENSIVE_THEME_DIRS": [
            "/openedx/themes/",
        ],
        "COMPREHENSIVE_THEME_LOCALE_PATHS": [],
        "CORS_ORIGIN_ALLOW_ALL": False,
        "COURSES_WITH_UNSAFE_CODE": [],
        "COURSES_INVITE_ONLY": True,
        "COURSE_ABOUT_VISIBILITY_PERMISSION": "see_exists",
        "COURSE_AUTHORING_MICROFRONTEND_URL": "/authoring",
        "COURSE_CATALOG_API_URL": "http://localhost:8008/api/v1",
        "COURSE_CATALOG_URL_ROOT": "http://localhost:8008",
        "COURSE_CATALOG_VISIBILITY_PERMISSION": "staff",
        "CREDENTIALS_INTERNAL_SERVICE_URL": "http://localhost:8005",
        "CREDENTIALS_PUBLIC_SERVICE_URL": "http://localhost:8005",
        "CREDIT_PROVIDER_SECRET_KEYS": {},  # pragma: allowlist secret
        "CSRF_COOKIE_SECURE": True,
        "CSRF_COOKIE_SAMESITE": "None",
        "CSRF_COOKIE_MASKED": False,
        "DASHBOARD_COURSE_LIMIT": None,
        "DATA_DIR": "/openedx/data",
        "DEFAULT_COURSE_VISIBILITY_IN_CATALOG": "both",
        "DEFAULT_FILE_STORAGE": "storages.backends.s3boto3.S3Boto3Storage",
        "DEFAULT_MOBILE_AVAILABLE": False,
        "DEPRECATED_ADVANCED_COMPONENT_TYPES": [],
        "EDXNOTES_INTERNAL_API": "http://localhost:18120/api/v1",
        "EDXNOTES_PUBLIC_API": "http://localhost:18120/api/v1",
        "EDX_PLATFORM_REVISION": "release",
        "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "EMAIL_HOST": "smtp.mailgun.org",
        "EMAIL_PORT": 587,
        "EMAIL_USE_TLS": True,
        "ENABLE_COMPREHENSIVE_THEMING": True,
        "ENABLE_MAX_FAILED_LOGIN_ATTEMPTS": False,
        "EVENT_TRACKING_SEGMENTIO_EMIT_WHITELIST": [],
        "EXTRA_MIDDLEWARE_CLASSES": [],
        "FACEBOOK_API_VERSION": "v2.1",
        "FACEBOOK_APP_ID": "FACEBOOK_APP_ID",
        "FACEBOOK_APP_SECRET": "FACEBOOK_APP_SECRET",  # pragma: allowlist secret
        "FEEDBACK_SUBMISSION_EMAIL": "",
        "FILE_UPLOAD_STORAGE_PREFIX": "submissions_attachments",
        "FINANCIAL_REPORTS": {
            "BUCKET": None,
            "ROOT_PATH": "sandbox",
            "STORAGE_TYPE": "localfs",
        },
        "FOOTER_ORGANIZATION_IMAGE": "/images/logo.png",
        "FORUM_SEARCH_BACKEND": "forum.search.es.ElasticsearchBackend",
        "FORUM_MONGODB_DATABASE": "forum",
        "GITHUB_REPO_ROOT": "/openedx/data",
        "HELP_TOKENS_BOOKS": {
            "course_author": "http://edx.readthedocs.io/projects/open-edx-building-and-running-a-course",
            "learner": "http://edx.readthedocs.io/projects/open-edx-learner-guide",
        },
        "ICP_LICENSE": None,
        "ICP_LICENSE_INFO": {},
        "ID_VERIFICATION_SUPPORT_LINK": "",
        "INTEGRATED_CHANNELS_API_CHUNK_TRANSMISSION_LIMIT": {
            "SAP": 1,
        },
        "JWT_EXPIRATION": 30,
        "LANGUAGE_CODE": "en",
        "LEARNER_PORTAL_URL_ROOT": "https://learner-portal-localhost:18000",
        "LEARNER_HOME_MICROFRONTEND_URL": "/dashboard/",
        "LOCAL_LOGLEVEL": "INFO",
        "LOGGING_ENV": "sandbox",
        "LOG_DIR": "/openedx/data/var/log/edx",
        "MAINTENANCE_BANNER_TEXT": "Sample banner message",
        "MEDIA_ROOT": "media/",
        "MEDIA_URL": "/media/",
        "MICROSITE_CONFIGURATION": {},
        "MKTG_URL_LINK_MAP": {
            "TOS": "tos",
            "ABOUT": "about",
            "ACCESSIBILITY": "accessibility",
        },
        "MOBILE_STORE_URLS": {},
        "NOTIFICATION_TYPE_ICONS": {},
        "DEFAULT_NOTIFICATION_ICON_URL": "",
        "OAUTH2_PROVIDER": {
            "OAUTH2_VALIDATOR_CLASS": "openedx.core.djangoapps.oauth_dispatch.dot_overrides.validators.EdxOAuth2Validator",
            "REFRESH_TOKEN_EXPIRE_SECONDS": 7776000,
            "SCOPES_BACKEND_CLASS": "openedx.core.djangoapps.oauth_dispatch.scopes.ApplicationModelScopes",
            "SCOPES": {
                "read": "Read access",
                "write": "Write access",
                "email": "Know your email address",
                "profile": "Know your name and username",
                "certificates:read": "Retrieve your course certificates",
                "grades:read": "Retrieve your grades for your enrolled courses",
                "tpa:read": "Retrieve your third-party authentication username mapping",
                "user_id": "Know your user identifier",
            },
            "DEFAULT_SCOPES": {
                "read": "Read access",
                "write": "Write access",
                "email": "Know your email address",
                "profile": "Know your name and username",
            },
            "REQUEST_APPROVAL_PROMPT": "auto_even_if_expired",
            "ERROR_RESPONSE_WITH_SCOPES": True,
        },
        "ORA2_FILE_PREFIX": "ora2",
        "PARTNER_SUPPORT_EMAIL": "",
        "PASSWORD_POLICY_COMPLIANCE_ROLLOUT_CONFIG": {
            "ENFORCE_COMPLIANCE_ON_LOGIN": False,
        },
        "PASSWORD_RESET_SUPPORT_LINK": "",
        "PLATFORM_FACEBOOK_ACCOUNT": "http://www.facebook.com/YourPlatformFacebookAccount",
        "PLATFORM_TWITTER_ACCOUNT": "@YourPlatformTwitterAccount",
        "POLICY_CHANGE_GRADES_ROUTING_KEY": "edx.lms.core.default",
        "PROCTORING_SETTINGS": {},
        "REDBEAT_KEY_PREFIX": "redbeat_lms",
        "REGISTRATION_EXTRA_FIELDS": {
            "city": "hidden",
            "confirm_email": "hidden",
            "country": "hidden",
            "gender": "optional",
            "goals": "optional",
            "honor_code": "hidden",
            "level_of_education": "optional",
            "mailing_address": "hidden",
            "terms_of_service": "hidden",
            "year_of_birth": "optional",
        },
        "RETIRED_EMAIL_DOMAIN": "retired.invalid",
        "RETIRED_EMAIL_PREFIX": "retired__user_",
        "RETIRED_USERNAME_PREFIX": "retired__user_",
        "RETIREMENT_SERVICE_WORKER_USERNAME": "retirement_worker",
        "SEGMENT_KEY": None,
        "SERVER_EMAIL": "odl-devops@mit.edu",
        "SESSION_COOKIE_SAMESITE": "None",
        "SESSION_COOKIE_SAMESITE_FORCE_ALL": True,
        "SESSION_COOKIE_SECURE": True,
        "SESSION_SAVE_EVERY_REQUEST": False,
        "SOCIAL_MEDIA_FOOTER_URLS": {},
        "SOCIAL_MEDIA_FOOTER_ACE_URLS": {},
        "SOCIAL_MEDIA_LOGO_URLS": {},
        "SOCIAL_SHARING_SETTINGS": {
            "CERTIFICATE_FACEBOOK": False,
            "CERTIFICATE_TWITTER": False,
            "CUSTOM_COURSE_URLS": False,
            "DASHBOARD_FACEBOOK": False,
            "DASHBOARD_TWITTER": False,
        },
        "STATIC_URL_BASE": "/static/",
        "STATIC_ROOT_BASE": "/openedx/staticfiles/",
        "STUDIO_NAME": "Studio",
        "STUDIO_SHORT_NAME": "Studio",
        "SUPPORT_SITE_LINK": None,
        "SYSTEM_WIDE_ROLE_CLASSES": [],
        "TECH_SUPPORT_EMAIL": "odl-devops@mit.edu",
        "TIME_ZONE": "America/New_York",
        "USERNAME_REPLACEMENT_WORKER": "OVERRIDE THIS WITH A VALID USERNAME",
        "USE_X_FORWARDED_HOST": True,  # Trust X-Forwarded-Host from APISIX proxy
        "VIDEO_IMAGE_MAX_AGE": 31536000,
        "VIDEO_IMAGE_SETTINGS": {
            "DIRECTORY_PREFIX": "video-images/",
            "STORAGE_CLASS": "storages.backends.s3boto3.S3Boto3Storage",
            "STORAGE_KWARGS": {
                "location": "media/",
            },
            "VIDEO_IMAGE_MAX_BYTES": 2097152,
            "VIDEO_IMAGE_MIN_BYTES": 2048,
            "BASE_URL": "/media/",
        },
        "VIDEO_TRANSCRIPTS_MAX_AGE": 31536000,
        "VIDEO_TRANSCRIPTS_SETTINGS": {
            "DIRECTORY_PREFIX": "video-transcripts/",
            "STORAGE_CLASS": "storages.backends.s3boto3.S3Boto3Storage",
            "STORAGE_KWARGS": {
                "location": "media/",
            },
            "VIDEO_TRANSCRIPTS_MAX_BYTES": 3145728,
            "BASE_URL": "/media/",
        },
        "VIDEO_UPLOAD_PIPELINE": {
            "BUCKET": "",
            "ROOT_PATH": "",
        },
        "WIKI_ENABLED": True,
        "XBLOCK_FS_STORAGE_BUCKET": None,
        "XBLOCK_FS_STORAGE_PREFIX": None,
        "XBLOCK_SETTINGS": {},
        "X_FRAME_OPTIONS": "DENY",
        "ZENDESK_API_KEY": "",
        "ZENDESK_CUSTOM_FIELDS": {},
        "ZENDESK_GROUP_ID_MAPPING": {},
        "ZENDESK_OAUTH_ACCESS_TOKEN": "",
        "ZENDESK_URL": "",
        "ZENDESK_USER": "",
        # ============================================================================
        # Module-level settings (extracted from FEATURES dict per openedx/envs/common.py)
        # These are no longer nested in the FEATURES dict
        # ============================================================================
        "AUTOPLAY_VIDEOS": False,
        "AUTOMATIC_AUTH_FOR_TESTING": False,
        "CUSTOM_COURSES_EDX": False,
        "DISABLE_START_DATES": False,
        "EMBARGO": True,
        "ENABLE_AUTOADVANCE_VIDEOS": False,
        "ENABLE_CORS_HEADERS": False,
        "ENABLE_COURSE_OLX_VALIDATION": False,
        "ENABLE_DISCUSSION_SERVICE": True,
        "ENABLE_EDXNOTES": True,
        "ENABLE_HELP_LINK": True,
        "ENABLE_MKTG_SITE": False,
        "ENABLE_MOBILE_REST_API": True,
        "ENABLE_OAUTH2_PROVIDER": True,
        "ENABLE_PUBLISHER": False,
        "ENABLE_SERVICE_STATUS": False,
        "ENABLE_SPECIAL_EXAMS": True,
        "ENABLE_TEAMS": True,
        "ENABLE_TEXTBOOK": True,
        "ENABLE_VIDEO_BUMPER": False,
        "FALLBACK_TO_ENGLISH_TRANSCRIPTS": True,
        "LICENSING": False,
        "MILESTONES_APP": False,
        "ENABLE_PREREQUISITE_COURSES": False,
        "RESTRICT_AUTOMATIC_AUTH": True,
        "SHOW_BUMPER_PERIODICITY": 7 * 24 * 3600,  # 7 days in seconds
        "SHOW_FOOTER_LANGUAGE_SELECTOR": False,
        "SHOW_HEADER_LANGUAGE_SELECTOR": False,
        "CERTIFICATES_HTML_VIEW": False,
        # ============================================================================
        # FEATURES dict - for remaining feature flags without module-level equivalents
        # ============================================================================
        "FEATURES": {
            "ALLOW_ALL_ADVANCED_COMPONENTS": True,
            "ALLOW_COURSE_STAFF_GRADE_DOWNLOADS": True,
            "ALLOW_HIDING_DISCUSSION_TAB": True,
            "ALLOW_PUBLIC_ACCOUNT_CREATION": True,
            "AUTH_USE_CERTIFICATES": False,
            "AUTH_USE_OPENID_PROVIDER": True,
            "BYPASS_ACTIVATION_EMAIL_FOR_EXTAUTH": True,
            "DISABLE_LOGIN_BUTTON": False,
            "ENABLE_AUTO_COURSE_REGISTRATION": True,
            "ENABLE_BLAKE2B_HASHING": True,
            "ENABLE_BULK_ENROLLMENT_VIEW": False,
            "ENABLE_NEW_BULK_EMAIL_EXPERIENCE": True,
            "ENABLE_COMBINED_LOGIN_REGISTRATION": True,
            "ENABLE_COUNTRY_ACCESS": False,
            "ENABLE_COURSE_BLOCKS_NAVIGATION_API": True,
            "ENABLE_COURSE_HOME_REDIRECT": True,
            "ENABLE_COURSEWARE_INDEX": False,
            "ENABLE_COURSEWARE_SEARCH": True,
            "ENABLE_CREDIT_API": False,
            "ENABLE_CREDIT_ELIGIBILITY": False,
            "ENABLE_CROSS_DOMAIN_CSRF_COOKIE": True,
            "ENABLE_CSMH_EXTENDED": True,
            "ENABLE_DISCUSSION_HOME_PANEL": True,
            "ENABLE_EDX_USERNAME_CHANGER": True,
            "ENABLE_ENROLLMENT_RESET": False,
            "ENABLE_EXPORT_GIT": True,
            "ENABLE_GIT_AUTO_EXPORT": True,
            "ENABLE_AUTO_GITHUB_REPO_CREATION": True,
            "ENABLE_GRADE_DOWNLOADS": True,
            "ENABLE_INSTRUCTOR_ANALYTICS": False,
            "ENABLE_INSTRUCTOR_EMAIL": True,
            "ENABLE_INSTRUCTOR_REMOTE_GRADEBOOK_CONTROLS": True,
            "ENABLE_LIBRARY_AUTHORING_MICROFRONTEND": True,
            "ENABLE_LIBRARY_INDEX": False,
            "ENABLE_ORA_USERNAMES_ON_DATA_EXPORT": False,
            "ENABLE_OTHER_COURSE_SETTINGS": True,
            "ENABLE_PAID_COURSE_REGISTRATION": False,
            "ENABLE_READING_FROM_MULTIPLE_HISTORY_TABLES": True,
            "ENABLE_RENDER_XBLOCK_API": True,
            "ENABLE_SHOPPING_CART": True,
            "ENABLE_SYSADMIN_DASHBOARD": True,
            "ENABLE_THIRD_PARTY_AUTH": True,
            "ENABLE_UNICODE_USERNAME": True,
            "ENABLE_VIDEO_UPLOAD_PIPELINE": True,
            "REQUIRE_COURSE_EMAIL_AUTH": False,
            "RESTRICT_ENROLL_BY_REG_METHOD": False,
            "SESSION_COOKIE_SECURE": True,
            "SKIP_EMAIL_VALIDATION": True,
        },
    }


def get_deployment_overrides(env_prefix: str) -> ConfigDict:
    """Get deployment-specific configuration overrides.

    Args:
        env_prefix: Environment prefix (mitx, xpro, mitxonline, mitx-staging)

    Returns:
        Configuration overrides for the specified deployment
    """
    # Shared configuration for mitx and mitx-staging
    mitx_shared_config = {
        "DEFAULT_SITE_THEME": env_prefix,  # "mitx" or "mitx-staging"
        "PLATFORM_NAME": "MITx Residential",
        "PLATFORM_DESCRIPTION": "MITx Residential",
        "PRESS_EMAIL": "support@mitx.mit.edu",
        "MITX_REDIRECT_ENABLED": False,
        "EMAIL_HOST": "outgoing.mit.edu",
        "EMAIL_PORT": 587,
        "EMAIL_USE_TLS": True,
        "COURSE_MODE_DEFAULTS": {
            "name": "Honor",
            "android_sku": None,
            "bulk_sku": None,
            "currency": "usd",
            "description": None,
            "expiration_datetime": None,
            "ios_sku": None,
            "min_price": 0,
            "sku": None,
            "slug": "honor",
            "suggested_prices": "",
        },
        "RETIREMENT_STATES": [
            "PENDING",
            "ERRORED",
            "ABORTED",
            "COMPLETE",
        ],
        # Module-level settings overrides for residential
        "DISABLE_START_DATES": False,
        "ENABLE_MKTG_SITE": False,  # Extracted to module-level
        # FEATURES overrides for residential (only non-module-level flags)
        "FEATURES": {
            "ALLOW_PUBLIC_ACCOUNT_CREATION": True,
            "DISABLE_HONOR_CERTIFICATES": True,
            "ENABLE_CANVAS_INTEGRATION": True,
            "ENABLE_CONTENT_LIBRARIES": True,
            "ENABLE_LTI_PROVIDER": True,
            "ENABLE_ORA_USERNAMES_ON_DATA_EXPORT": False,
            "ENABLE_RAPID_RESPONSE_AUTHOR_VIEW": True,
            "ENABLE_THIRD_PARTY_ONLY_AUTH": True,
            "MAX_PROBLEM_RESPONSES_COUNT": 10000,
            "REROUTE_ACTIVATION_EMAIL": "mitx-support@mit.edu",
        },
        "BULK_EMAIL_DEFAULT_RETRY_DELAY": 30,
        "BULK_EMAIL_MAX_RETRIES": 5,
        "X_FRAME_OPTIONS": "ALLOW-FROM canvas.mit.edu",
    }
    mitx = {"SYSADMIN_DEFAULT_BRANCH": "live", **mitx_shared_config}
    mitx.update(
        {
            "COURSE_CATALOG_VISIBILITY_PERMISSION": "staff",
            "COURSES_INVITE_ONLY": False,
        }
    )

    # Deployment-specific overrides
    deployment_overrides = {
        "mitx": mitx,
        "mitx-staging": {"SYSADMIN_DEFAULT_BRANCH": "master", **mitx_shared_config},
        "xpro": {
            "DEFAULT_SITE_THEME": "xpro",
            "PLATFORM_NAME": "MIT xPRO",
            "PLATFORM_DESCRIPTION": "MIT xPRO",
            "PRESS_EMAIL": "support@xpro.mit.edu",
            "MITX_REDIRECT_ENABLED": True,
            "MITX_REDIRECT_ALLOW_RE_LIST": [
                "^/(admin|auth|logout|register|api|oauth2|user_api|heartbeat|login_refresh|c4x|asset-v1:|assets/courseware/)",
                "^/courses/.*/xblock/.*/handler_noauth/outcome_service_handler",
                "^/courses/.*/courseware-navigation-sidebar/toggles/?$",
                "^/courses/.*/courseware-search/enabled/?$",
            ],
            "EMAIL_HOST": "smtp.mailgun.org",
            "EMAIL_PORT": 587,
            "EMAIL_USE_TLS": True,
            "COURSE_MODE_DEFAULTS": {
                "name": "Professional",
                "android_sku": None,
                "bulk_sku": None,
                "currency": "usd",
                "description": None,
                "expiration_datetime": None,
                "ios_sku": None,
                "min_price": 0,
                "sku": None,
                "slug": "no-id-professional",
                "suggested_prices": "",
            },
            "RETIREMENT_STATES": [
                "PENDING",
                "RETIRING_FORUMS",
                "FORUMS_COMPLETE",
                "RETIRING_ENROLLMENTS",
                "ENROLLMENTS_COMPLETE",
                "RETIRING_NOTES",
                "NOTES_COMPLETE",
                "RETIRING_PROCTORING",
                "PROCTORING_COMPLETE",
                "RETIRING_LMS_MISC",
                "LMS_MISC_COMPLETE",
                "RETIRING_LMS",
                "LMS_COMPLETE",
                "ERRORED",
                "ABORTED",
                "COMPLETE",
            ],
            # Module-level settings overrides for xpro
            "ENABLE_MKTG_SITE": True,  # Extracted to module-level
            "LICENSING": True,  # Extracted to module-level
            # FEATURES overrides for xpro (only non-module-level flags)
            "FEATURES": {
                "ENABLE_LTI_PROVIDER": False,
                "ENABLE_ORA_USERNAMES_ON_DATA_EXPORT": True,
                "ENABLE_SYSADMIN_DASHBOARD": True,
                "REROUTE_ACTIVATION_EMAIL": "support@xpro.mit.edu",
            },
        },
        "mitxonline": {
            "AWS_SES_SEND_MESSAGE_TAGS": {"edxapp-mitxonline": "true"},
            "DEFAULT_SITE_THEME": "mitxonline",
            "PLATFORM_NAME": "MIT Learn",
            "PLATFORM_DESCRIPTION": "MIT Learn",
            "PRESS_EMAIL": "support@mitxonline.mit.edu",
            "MITX_REDIRECT_ENABLED": True,
            "MITX_REDIRECT_ALLOW_RE_LIST": [
                "^/(admin|auth|logout|register|api|oauth2|user_api|heartbeat|login_refresh|c4x|asset-v1:|assets/courseware/|lti_provider)",
                "^/courses/.*/xblock/.*/handler_noauth/outcome_service_handler",
                "^/v1/accounts/bulk_retire_users",
                "^/courses/course-v1:.*?/xqueue/.*$",
                "^/courses/.*/courseware-navigation-sidebar/toggles/?$",
                "^/courses/.*/courseware-search/enabled/?$",
            ],
            "SYSADMIN_DEFAULT_BRANCH": "live",
            "EMAIL_BACKEND": "django_ses.SESBackend",
            "MIT_BASE_URL": "https://web.mit.edu",
            "MIT_LEARN_SUPPORT_SITE_LINK": "mailto:mitlearn-support@mit.edu",
            "UAI_COURSE_KEY_FORMATS": [
                "course-v1:uai_",
                "course-v1:mitxt+ctl.scx_wm+1t2026",
            ],
            "OL_OPENEDX_COURSE_SYNC_SERVICE_WORKER_USERNAME": "studio_worker",
            "ORA2_FILE_PREFIX": "mitxonline/ora2",
            "SUPPORT_SITE_LINK": "https://mitxonline.zendesk.com/hc/",
            "REGISTRATION_EXTRA_FIELDS": {
                "honor_code": "required",
            },
            "RETIRED_EMAIL_PREFIX": "retired__user__",
            "RETIRED_USERNAME_PREFIX": "retired__user__",
            "RETIREMENT_SERVICE_WORKER_USERNAME": "retirement_service_worker",
            "RETIREMENT_STATES": [
                "PENDING",
                "RETIRING_FORUMS",
                "FORUMS_COMPLETE",
                "RETIRING_ENROLLMENTS",
                "ENROLLMENTS_COMPLETE",
                "RETIRING_NOTES",
                "NOTES_COMPLETE",
                "RETIRING_PROCTORING",
                "PROCTORING_COMPLETE",
                "RETIRING_LMS_MISC",
                "LMS_MISC_COMPLETE",
                "RETIRING_LMS",
                "LMS_COMPLETE",
                "ERRORED",
                "ABORTED",
                "COMPLETE",
            ],
            "X_FRAME_OPTIONS": "ALLOW-FROM canvas.mit.edu",
            "CELERYBEAT_SCHEDULE": {
                "send-email-digest": {
                    "task": "openedx.core.djangoapps.notifications.email.tasks.send_digest_email_to_all_users",
                    "schedule": 86400,
                    "args": ["Daily"],
                },
            },
            # Module-level settings overrides for mitxonline
            "ENABLE_MKTG_SITE": True,  # Extracted to module-level
            "LICENSING": True,  # Extracted to module-level
            "MILESTONES_APP": True,  # Extracted to module-level
            # FEATURES overrides for mitxonline (only non-module-level flags)
            "FEATURES": {
                "ALLOW_COURSE_STAFF_GRADE_DOWNLOADS": True,
                "ENABLE_EXAM_SETTINGS_HTML_VIEW": True,
                "ENABLE_FORUM_DAILY_DIGEST": True,
                "ENABLE_LTI_PROVIDER": True,
                "ENABLE_V2_CERT_DISPLAY_SETTINGS": True,
                "ENABLE_BULK_USER_RETIREMENT": True,
            },
            "OAUTH2_PROVIDER": {
                "ALLOWED_REDIRECT_URI_SCHEMES": [
                    "https",
                    "edu.mit.learn.app",
                ],
            },
            "SENTRY_IGNORED_EXCEPTION_MESSAGES": [
                (
                    "Tried to inspect an unsupported, broken, or",
                    "missing downstream->upstream link:",
                ),
                (
                    "Invalid HTTP_HOST header: 'vqbjqfz3ldd42z6qff3t2h5cr62c5rok._domainkey.huggingface.co'.",
                    "The domain name provided is not valid according to RFC 1034/1035.",
                ),
                (
                    "A label was requested for language code `ht` but",
                    "the code is completely unknown",
                ),
                ("Failed async course content export to git",),
                ("Failed to pull git repository",),
            ],
        },
    }

    return deployment_overrides.get(env_prefix, {})


def build_general_config(env_prefix: str) -> ConfigDict:
    """Build complete 50-general-config.yaml for a deployment.

    Args:
        env_prefix: Environment prefix (mitx, xpro, mitxonline, mitx-staging)

    Returns:
        Complete configuration dictionary for the deployment
    """
    base = build_base_general_config()
    overrides = get_deployment_overrides(env_prefix)

    # Merge FEATURES and OAUTH2_PROVIDER nested dicts properly
    return deep_merge(base, overrides)


def render_yaml(config: ConfigDict) -> str:
    """Render configuration dictionary to YAML string.

    Args:
        config: Configuration dictionary to render

    Returns:
        YAML string representation
    """

    # Create custom SafeDumper that ignores aliases to prevent duplicate anchor errors
    class NoAliasSafeDumper(yaml.SafeDumper):
        pass

    # Override ignore_aliases to disable YAML anchors/aliases
    NoAliasSafeDumper.ignore_aliases = lambda self, data: True  # type: ignore[method-assign]  # noqa: ARG005

    # Use safe_dump with custom dumper and explicit settings for consistent formatting
    return yaml.dump(
        config,
        Dumper=NoAliasSafeDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=1000,  # Prevent line wrapping
    )
