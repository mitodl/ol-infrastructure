# ruff: noqa: E501, PLR0913, FBT003
"""Refactored Kubernetes ConfigMap generation for EDXApp using dictionary-based configuration.

This module replaces the nested f-string templating with a structured,
maintainable dictionary-based configuration system that supports per-deployment
customization while reducing duplication across the 4 EDX deployments.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pulumi_kubernetes as kubernetes
import yaml
from pulumi import Config, Output, ResourceOptions, StackReference

from ol_infrastructure.applications.edxapp.config_builder import (
    build_general_config,
    render_yaml,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.lib.pulumi_helper import StackInfo


def _build_interpolated_config_dict(
    stack_info: StackInfo,
    edxapp_config: Config,
    runtime_config: dict[str, str],
    storage_bucket_name: str,
    course_bucket_name: str,
    grades_bucket_name: str,
    ses_configuration_set: str,
    env_name: str,
) -> dict[str, Any]:
    """Build the 60-interpolated-config.yaml as a dictionary.

    This replaces nested f-strings with a structured dictionary approach,
    making conditional logic and deployment-specific settings explicit
    and maintainable.

    Args:
        stack_info: Stack information with environment details
        edxapp_config: Pulumi config for edxapp
        runtime_config: Runtime configuration (Redis, OpenSearch hosts)
        storage_bucket_name: S3 bucket for storage
        course_bucket_name: S3 bucket for courses
        grades_bucket_name: S3 bucket for grades
        ses_configuration_set: SES configuration set name
        env_name: Environment name (e.g., "mitx-production")

    Returns:
        Configuration dictionary for 60-interpolated-config.yaml
    """
    domains = edxapp_config.require_object("domains")

    # Determine marketing domain based on deployment type
    marketing_domain = (
        edxapp_config.get("mitxonline_domain")
        if stack_info.env_prefix == "mitxonline"
        else edxapp_config.get("marketing_domain")
    )

    # Build base interpolated config shared across deployments
    config: dict[str, Any] = {
        "ALLOWED_HOSTS": [
            domains["lms"],
            domains["preview"],
            domains["studio"],
            edxapp_config.require("backend_lms_domain"),
            edxapp_config.require("backend_studio_domain"),
            edxapp_config.require("backend_preview_domain"),
        ],
        "AWS_S3_CUSTOM_DOMAIN": f"{storage_bucket_name}.s3.amazonaws.com",
        "AWS_STORAGE_BUCKET_NAME": storage_bucket_name,
        "AWS_SES_CONFIGURATION_SET": ses_configuration_set,
        "BASE_COOKIE_DOMAIN": domains["lms"],
        "BLOCK_STRUCTURES_SETTINGS": {
            "COURSE_PUBLISH_TASK_DELAY": 30,
            "PRUNING_ACTIVE": True,
            "TASK_DEFAULT_RETRY_DELAY": 30,
            "TASK_MAX_RETRIES": 5,
            "STORAGE_CLASS": "storages.backends.s3boto3.S3Boto3Storage",
            "DIRECTORY_PREFIX": "coursestructure/",
            "STORAGE_KWARGS": {
                "bucket_name": storage_bucket_name,
                "default_acl": "public-read",
            },
        },
        "EMAIL_USE_COURSE_ID_FROM_FOR_BULK": edxapp_config.get_bool(
            "email_use_course_id_from_for_bulk", False
        ),
        "BULK_EMAIL_DEFAULT_FROM_EMAIL": (
            edxapp_config.get("bulk_email_default_from_email")
            or edxapp_config.require("sender_email_address")
        ),
        "CELERY_BROKER_HOSTNAME": runtime_config["redis_hostname"],
        "CMS_BASE": domains["studio"],
        "CONTACT_EMAIL": edxapp_config.require("sender_email_address"),
        "CORS_ORIGIN_WHITELIST": [
            f"https://{domains['lms']}",
            f"https://{domains['studio']}",
            f"https://{domains['preview']}",
            f"https://{marketing_domain}",
            f"https://{runtime_config['notes_domain']}",
            f"https://{edxapp_config.require('learn_ai_frontend_domain')}",
            f"https://{edxapp_config.require('mit_learn_domain')}",
            f"https://{env_name}-edxapp-storage.s3.amazonaws.com",
            "https://idp.mit.edu",
        ],
        "COURSE_IMPORT_EXPORT_BUCKET": course_bucket_name,
        "CROSS_DOMAIN_CSRF_COOKIE_DOMAIN": domains["lms"],
        "CROSS_DOMAIN_CSRF_COOKIE_NAME": f"{env_name}-edxapp-csrftoken",
        "DEFAULT_FEEDBACK_EMAIL": edxapp_config.require("sender_email_address"),
        "DEFAULT_FROM_EMAIL": edxapp_config.require("sender_email_address"),
        "DISCUSSIONS_MICROFRONTEND_URL": f"https://{domains['lms']}/discuss",
        "EDXMKTG_USER_INFO_COOKIE_NAME": f"{env_name}-edx-user-info",
        "EDXNOTES_INTERNAL_API": f"https://{runtime_config['notes_domain']}/api/v1",
        "EDXNOTES_PUBLIC_API": f"https://{runtime_config['notes_domain']}/api/v1",
        "ELASTIC_SEARCH_CONFIG": [
            {
                "host": runtime_config["opensearch_hostname"],
                "port": 443,
                "use_ssl": True,
            }
        ],
        "ELASTIC_SEARCH_CONFIG_ES7": [
            {
                "host": runtime_config["opensearch_hostname"],
                "port": 443,
                "use_ssl": True,
            }
        ],
        "FILE_UPLOAD_STORAGE_BUCKET_NAME": storage_bucket_name,
        "FORUM_ELASTIC_SEARCH_CONFIG": [
            {
                "host": runtime_config["opensearch_hostname"],
                "port": "443",
                "use_ssl": True,
            }
        ],
        "FORUM_MONGODB_DATABASE": "forum",
        "GITHUB_REPO_ROOT": "/openedx/data",
        "GOOGLE_ANALYTICS_ACCOUNT": edxapp_config.require("google_analytics_id"),
        "GRADES_DOWNLOAD": {
            "BUCKET": grades_bucket_name,
            "ROOT_PATH": "grades",
            "STORAGE_CLASS": "django.core.files.storage.S3Storage",
            "STORAGE_KWARGS": {
                "location": "grades/",
            },
            "STORAGE_TYPE": "S3",
        },
        "LANGUAGE_COOKIE": f"{env_name}-openedx-language-preference",
        "MIT_LEARN_AI_API_URL": f"https://{edxapp_config.require('mit_learn_api_domain')}/ai",
        "MIT_LEARN_API_BASE_URL": f"https://{edxapp_config.require('mit_learn_api_domain')}/learn",
        "MIT_LEARN_SUMMARY_FLASHCARD_URL": f"https://{edxapp_config.require('mit_learn_api_domain')}/learn/api/v1/contentfiles/",
        "MIT_LEARN_BASE_URL": f"https://{edxapp_config.require('mit_learn_domain')}",
        "MIT_LEARN_AI_XBLOCK_CHAT_API_URL": f"https://{edxapp_config.require('mit_learn_api_domain')}/ai/http/canvas_syllabus_agent/",
        "MIT_LEARN_AI_XBLOCK_TUTOR_CHAT_API_URL": f"https://{edxapp_config.require('mit_learn_api_domain')}/ai/http/canvas_tutor_agent/",
        "MIT_LEARN_AI_XBLOCK_PROBLEM_SET_LIST_URL": f"https://{edxapp_config.require('mit_learn_api_domain')}/ai/api/v0/problem_set_list",
        "MIT_LEARN_AI_XBLOCK_CHAT_RATING_URL": f"https://{edxapp_config.require('mit_learn_api_domain')}/ai/api/v0/chat_sessions/",
        "MIT_LEARN_LOGO": f"https://{domains['lms']}/static/mitxonline/images/mit-learn-logo.svg",
        "LEARNING_MICROFRONTEND_URL": f"https://{domains['lms']}/learn",
        "LMS_BASE": domains["lms"],
        "LMS_INTERNAL_ROOT_URL": f"https://{domains['lms']}",
        "LMS_ROOT_URL": f"https://{domains['lms']}",
        "LOGIN_REDIRECT_WHITELIST": [
            domains["studio"],
            domains["lms"],
            domains["preview"],
            marketing_domain,
            edxapp_config.get("mit_learn_domain"),
        ],
        "LOGO_URL": f"https://{domains['lms']}/static/{stack_info.env_prefix}/images/logo.svg",
        "LOGO_URL_PNG_FOR_EMAIL": f"https://{domains['lms']}/static/{stack_info.env_prefix}/images/logo.png",
        "LOGO_TRADEMARK_URL": f"https://{domains['lms']}/static/{stack_info.env_prefix}/images/{('mit-ol-logo' if stack_info.env_prefix == 'xpro' else 'mit-logo')}.svg",
        "MARKETING_SITE_BASE_URL": f"https://{marketing_domain}/",
        "MARKETING_SITE_CHECKOUT_URL": f"https://{marketing_domain}/cart/add/",
        "NOTIFICATIONS_DEFAULT_FROM_EMAIL": (
            edxapp_config.get("bulk_email_default_from_email")
            or edxapp_config.require("sender_email_address")
        ),
        "PAYMENT_SUPPORT_EMAIL": edxapp_config.require("sender_email_address"),
        "PREVIEW_LMS_BASE": domains["preview"],
        "SENTRY_ENVIRONMENT": env_name,
        "SESSION_COOKIE_DOMAIN": f".{domains['lms'].split('.', 1)[-1]}",
        "UNIVERSITY_EMAIL": edxapp_config.require("sender_email_address"),
        "OPENEDX_TELEMETRY": ["edx_django_utils.monitoring.OpenTelemetryBackend"],
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://grafana-k8s-monitoring-alloy-receiver.grafana.svc.cluster.local:4318",
        "OTEL_SERVICE_NAME": env_name + "-edxapp",
        "OTEL_LOG_LEVEL": "info",
        "ECOMMERCE_PUBLIC_URL_ROOT": domains["lms"],
    }

    # Residential-specific configuration (mitx, mitx-staging)
    if stack_info.env_prefix in ["mitx", "mitx-staging"]:
        config["CSRF_TRUSTED_ORIGINS"] = [
            "https://canvas.mit.edu",
            f"https://{domains['lms']}",
        ]
        config.update(
            {
                "CANVAS_BASE_URL": edxapp_config.require("canvas_base_url"),
                "MKTG_URL_OVERRIDES": {
                    "COURSES": f"https://{marketing_domain}/",
                    "PRIVACY": f"https://{marketing_domain}/privacy",
                    "TOS": f"https://{marketing_domain}/terms",
                    "ABOUT": f"https://{marketing_domain}/about",
                    "HONOR": f"https://{marketing_domain}/honor-code/",
                    "ACCESSIBILITY": "https://accessibility.mit.edu/",
                    "CONTACT": f"https://{stack_info.env_prefix}.zendesk.com/hc/en-us/requests/new/",
                    "TOS_AND_HONOR": "",
                },
                "IDA_LOGOUT_URI_LIST": [
                    f"https://{marketing_domain}/logout",
                    f"https://{domains['studio']}/logout",
                    f"https://{edxapp_config.require('mit_learn_api_domain')}/logout",
                ],
                "MKTG_URLS": {
                    "ROOT": f"https://{marketing_domain}/",
                },
            }
        )

    # xPro-specific configuration
    elif stack_info.env_prefix == "xpro":
        config["CSRF_TRUSTED_ORIGINS"] = [f"https://{domains['lms']}"]
        config.update(
            {
                "XPRO_BASE_URL": f"https://{marketing_domain}",
                "MKTG_URL_OVERRIDES": {
                    "COURSES": f"https://{marketing_domain}/catalog/",
                    "PRIVACY": f"https://{marketing_domain}/privacy-policy/",
                    "TOS": f"https://{marketing_domain}/terms-of-service/",
                    "ABOUT": f"https://{marketing_domain}/about-us",
                    "HONOR": f"https://{marketing_domain}/honor-code/",
                    "ACCESSIBILITY": "https://accessibility.mit.edu/",
                    "CONTACT": f"https://{stack_info.env_prefix}.zendesk.com/hc/en-us/requests/new/",
                    "TOS_AND_HONOR": "",
                },
                "IDA_LOGOUT_URI_LIST": [
                    f"https://{marketing_domain}/logout",
                    f"https://{domains['studio']}/logout",
                    f"https://{edxapp_config.require('mit_learn_api_domain')}/logout",
                ],
                "MKTG_URLS": {
                    "ROOT": f"https://{marketing_domain}/",
                },
            }
        )

    # MITx Online-specific configuration
    elif stack_info.env_prefix == "mitxonline":
        config["CSRF_TRUSTED_ORIGINS"] = [f"https://{domains['lms']}"]
        config.update(
            {
                "MITXONLINE_BASE_URL": f"https://{marketing_domain}/",
                "IDA_LOGOUT_URI_LIST": [
                    f"https://{marketing_domain}/logout",
                    f"https://{domains['studio']}/logout",
                    f"https://{edxapp_config.require('mit_learn_api_domain')}/logout",
                ],
                "MKTG_URLS": {
                    "ROOT": f"https://{marketing_domain}/",
                },
                "MKTG_URL_OVERRIDES": {
                    "TOS": f"https://{edxapp_config.require('mit_learn_domain')}/terms",
                    "ABOUT": f"https://{edxapp_config.require('mit_learn_domain')}/about",
                    "ACCESSIBILITY": "https://accessibility.mit.edu/",
                    "CONTACT": f"https://{stack_info.env_prefix}.zendesk.com/hc/en-us/requests/new/",
                    "TOS_AND_HONOR": "",
                },
            }
        )

    return config


@dataclass
class EdxappConfigMaps:
    """Container for all EDXApp configuration maps."""

    general: kubernetes.core.v1.ConfigMap
    interpolated: Output
    cms_general: kubernetes.core.v1.ConfigMap
    cms_interpolated: kubernetes.core.v1.ConfigMap
    lms_general: kubernetes.core.v1.ConfigMap
    lms_interpolated: kubernetes.core.v1.ConfigMap
    uwsgi_ini: kubernetes.core.v1.ConfigMap
    waffle_flags_yaml: kubernetes.core.v1.ConfigMap

    general_config_name: str
    interpolated_config_name: str
    cms_general_config_name: str
    cms_interpolated_config_name: str
    lms_general_config_name: str
    lms_interpolated_config_name: str
    uwsgi_ini_config_name: str
    waffle_flags_yaml_config_name: str


def create_k8s_configmaps(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    edxapp_config: Config,
    edxapp_cache: OLAmazonCache,
    notes_stack: StackReference,
    opensearch_hostname: Output[str],
) -> EdxappConfigMaps:
    """Create all Kubernetes configmaps for EDXApp using dictionary-based configuration.

    Args:
        stack_info: Stack information
        namespace: Kubernetes namespace
        k8s_global_labels: Global Kubernetes labels
        edxapp_config: Pulumi config for edxapp
        edxapp_cache: Redis cache instance
        notes_stack: StackReference for notes service
        opensearch_hostname: OpenSearch hostname

    Returns:
        EdxappConfigMaps dataclass containing all ConfigMap resources
    """
    general_config_name = "50-general-config-yaml"

    # Build general config from dictionary (replaces YAML file)
    general_config_dict = build_general_config(stack_info.env_prefix)
    general_config_yaml = render_yaml(general_config_dict)

    general_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-general-config-{stack_info.env_suffix}",
        metadata={
            "name": general_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "50-general-config.yaml": general_config_yaml,
        },
    )

    # Misc values needed for the next step
    env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
    course_bucket_name = f"{env_name}-edxapp-courses"
    grades_bucket_name = f"{env_name}-edxapp-grades"
    storage_bucket_name = f"{env_name}-edxapp-storage"
    ses_configuration_set = f"edxapp-{env_name}"

    # Load interpolated configuration from dictionary
    interpolated_config_name = "60-interpolated-config-yaml"
    interpolated_config_map = Output.all(
        redis_hostname=edxapp_cache.address,
        opensearch_hostname=opensearch_hostname,
        notes_domain=notes_stack.require_output("notes_domain"),
    ).apply(
        lambda runtime_config: kubernetes.core.v1.ConfigMap(
            f"ol-{stack_info.env_prefix}-edxapp-interpolated-config-{stack_info.env_suffix}",
            metadata={
                "name": interpolated_config_name,
                "namespace": namespace,
                "labels": k8s_global_labels,
            },
            data={
                "60-interpolated-config.yaml": render_yaml(
                    _build_interpolated_config_dict(
                        stack_info=stack_info,
                        edxapp_config=edxapp_config,
                        runtime_config=runtime_config,
                        storage_bucket_name=storage_bucket_name,
                        course_bucket_name=course_bucket_name,
                        grades_bucket_name=grades_bucket_name,
                        ses_configuration_set=ses_configuration_set,
                        env_name=env_name,
                    )
                ),
            },
            opts=ResourceOptions(delete_before_replace=True),
        )
    )

    # CMS general configuration
    cms_general_config_name = "71-cms-general-config-yaml"
    cms_general_config_content: dict[str, Any] = {
        "COURSE_AUTHORING_MICROFRONTEND_URL": "/authoring",
        "DISCUSSIONS_INCONTEXT_LEARNMORE_URL": "https://openedx.atlassian.net/wiki/spaces/COMM/pages/3470655498/Discussions+upgrade+Sidebar+and+new+topic+structure",
        "GIT_REPO_EXPORT_DIR": "/openedx/data/export_course_repos",
        "GIT_EXPORT_DEFAULT_IDENT": {
            "name": "MITx Online",
            "email": "mitx-devops@mit.edu",
        },
        "PARSE_KEYS": {},
        "CALCULATOR_HELP_URL": "",
        "DISCUSSIONS_HELP_URL": "",
        "EDXNOTES_HELP_URL": "",
        "PROGRESS_HELP_URL": "",
        "TEAMS_HELP_URL": "",
        "TEXTBOOKS_HELP_URL": "",
        "WIKI_HELP_URL": "",
        "CUSTOM_PAGES_HELP_URL": "",
        "COURSE_LIVE_HELP_URL": "",
        "ORA_SETTINGS_HELP_URL": "https://edx.readthedocs.io/projects/open-edx-building-and-running-a-course/en/latest/course_assets/pages.html#configuring-course-level-open-response-assessment-settings",
    }

    # Add CMS-specific features for residential deployments
    if stack_info.env_prefix in ["mitx", "mitx-staging"]:
        cms_general_config_content["FEATURES"] = {
            "ENABLE_INSTRUCTOR_BACKGROUND_TASKS": True,
            "ENABLE_NEW_BULK_EMAIL_EXPERIENCE": True,
            "MAX_PROBLEM_RESPONSES_COUNT": 10000,
        }
        cms_general_config_content["SEGMENT_IO"] = False

    cms_general_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-cms-general-config-{stack_info.env_suffix}",
        metadata={
            "name": cms_general_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "71-cms-general-config.yaml": render_yaml(cms_general_config_content),
        },
    )

    # CMS interpolated configuration
    cms_interpolated_config_name = "72-cms-interpolated-config-yaml"
    cms_interpolated_config = {
        "SITE_NAME": edxapp_config.require_object("domains")["studio"],
        "SOCIAL_AUTH_EDX_OAUTH2_URL_ROOT": f"https://{edxapp_config.require_object('domains')['lms']}",
        "SOCIAL_AUTH_EDX_OAUTH2_PUBLIC_URL_ROOT": f"https://{edxapp_config.require_object('domains')['lms']}",
        "SESSION_COOKIE_NAME": f"{env_name}-edx-studio-sessionid",
    }

    if stack_info.env_prefix == "mitxonline":
        cms_interpolated_config["GITHUB_ORG_API_URL"] = edxapp_config.require(
            "github_org_api_url"
        )

    cms_interpolated_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-cms-interpolated-config-{stack_info.env_suffix}",
        metadata={
            "name": cms_interpolated_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={"72-cms-interpolated-config.yaml": render_yaml(cms_interpolated_config)},
        opts=ResourceOptions(delete_before_replace=True),
    )

    # LMS general configuration
    lms_general_config_name = "81-lms-general-config-yaml"
    lms_general_config_content = {
        "ACCOUNT_MICROFRONTEND_URL": None,
        "ACE_CHANNEL_DEFAULT_EMAIL": "django_email",
        "ACE_CHANNEL_TRANSACTIONAL_EMAIL": "django_email",
        "ACE_ENABLED_CHANNELS": ["django_email"],
        "ACE_ENABLED_POLICIES": ["bulk_email_optout"],
        "ACE_ROUTING_KEY": "edx.lms.core.default",
        "ADMIN": ["MITx Stacktrace Recipients", "cuddle-bunnies@mit.edu"],
        "API_ACCESS_FROM_EMAIL": "api-requests@example.com",
        "API_ACCESS_MANAGER_EMAIL": "api-access@example.com",
        "API_DOCUMENTATION_URL": "http://course-catalog-api-guide.readthedocs.io/en/latest/",
        "AUDIT_CERT_CUTOFF_DATE": None,
        "AUTH_DOCUMENTATION_URL": "http://course-catalog-api-guide.readthedocs.io/en/latest/authentication/index.html",
        "BULK_EMAIL_ROUTING_KEY_SMALL_JOBS": "edx.lms.core.default",
        "COMMUNICATIONS_MICROFRONTEND_URL": "/communications",
        "CONTACT_MAILING_ADDRESS": "SET-ME-PLEASE",
        "CREDIT_HELP_LINK_URL": "",
        "DCS_SESSION_COOKIE_SAMESITE": "Lax",
        "DCS_SESSION_COOKIE_SAMESITE_FORCE_ALL": True,
        "ENABLE_INSTRUCTOR_LEGACY_DASHBOARD": True,
        "GIT_REPO_DIR": "/openedx/data/export_course_repos",
        "GOOGLE_ANALYTICS_LINKEDIN": "",
        "GOOGLE_ANALYTICS_TRACKING_ID": "",
        "GOOGLE_SITE_VERIFICATION_ID": "",
        "HTTPS": "on",
        "LEARNER_HOME_MFE_REDIRECT_PERCENTAGE": 100,
        "LEARNER_HOME_MICROFRONTEND_URL": "/dashboard/",
        "LTI_AGGREGATE_SCORE_PASSBACK_DELAY": 900,
        "LTI_USER_EMAIL_DOMAIN": "lti.example.com",
        "MAILCHIMP_NEW_USER_LIST_ID": None,
        "OAUTH_DELETE_EXPIRED": True,
        "OAUTH_ENFORCE_SECURE": True,
        "OAUTH_EXPIRE_CONFIDENTIAL_CLIENT_DAYS": 365,
        "OAUTH_EXPIRE_PUBLIC_CLIENT_DAYS": 30,
        "OPTIMIZELY_PROJECT_ID": None,
        "ORA_GRADING_MICROFRONTEND_URL": "/ora-grading",
        "ORDER_HISTORY_MICROFRONTEND_URL": None,
        "ORGANIZATIONS_AUTOCREATE": True,
        "PAID_COURSE_REGISTRATION_CURRENCY": ["usd", "$"],
        "PARENTAL_CONSENT_AGE_LIMIT": 13,
        "PDF_RECEIPT_BILLING_ADDRESS": "Enter your receipt billing\n\n    address here.\n\n    ",
        "PDF_RECEIPT_COBRAND_LOGO_PATH": "",
        "PDF_RECEIPT_DISCLAIMER_TEXT": "ENTER YOUR RECEIPT DISCLAIMER TEXT HERE.\n\n    ",
        "PDF_RECEIPT_FOOTER_TEXT": "Enter your receipt footer text here.\n\n    ",
        "PDF_RECEIPT_LOGO_PATH": "",
        "PDF_RECEIPT_TAX_ID": "00-0000000",
        "PDF_RECEIPT_TAX_ID_LABEL": "fake Tax ID",
        "PDF_RECEIPT_TERMS_AND_CONDITIONS": "Enter your receipt terms and conditions here.\n\n    ",
        "PROFILE_IMAGE_BACKEND": {
            "class": "openedx.core.storage.OverwriteStorage",
            "options": {
                "base_url": "/media/profile-images/",
                "location": "/openedx/data/var/edxapp/media/profile-images/",
            },
        },
        "PROFILE_IMAGE_MAX_BYTES": 1048576,
        "PROFILE_IMAGE_MIN_BYTES": 100,
        "PROFILE_IMAGE_SECRET_KEY": "placeholder_secret_key",  # pragma: allowlist secret
        "PROFILE_IMAGE_SIZES_MAP": {
            "full": 500,
            "large": 120,
            "medium": 50,
            "small": 30,
        },
        "PROFILE_MICROFRONTEND_URL": None,
        "PROGRAM_CERTIFICATES_ROUTING_KEY": "edx.lms.core.default",
        "PROGRAM_CONSOLE_MICROFRONTEND_URL": None,
        "REGISTRATION_VALIDATION_RATELIMIT": "1000000/minute",
        "REGISTRATION_RATELIMIT": "1000000/minute",
        "RATELIMIT_RATE": "600/m",
        "RECALCULATE_GRADES_ROUTING_KEY": "edx.lms.core.default",
        "RESTRICT_ENROLL_SOCIAL_PROVIDERS": ["mit-kerberos"],
        "STUDENT_FILEUPLOAD_MAX_SIZE": 52428800,
        "THIRD_PARTY_AUTH_BACKENDS": [
            "common.djangoapps.third_party_auth.saml.SAMLAuthBackend",
            "common.djangoapps.third_party_auth.lti.LTIAuthBackend",
        ],
        "TRACKING_SEGMENTIO_WEBHOOK_SECRET": "",
        "VERIFY_STUDENT": {
            "DAYS_GOOD_FOR": 365,
            "EXPIRING_SOON_WINDOW": 28,
        },
        "VIDEO_CDN_URL": {
            "EXAMPLE_COUNTRY_CODE": "http://example.com/edx/video?s3_url=",
        },
        "WRITABLE_GRADEBOOK_URL": "/gradebook",
    }

    # Add residential-specific LMS features
    if stack_info.env_prefix in ["mitx", "mitx-staging"]:
        lms_general_config_content["FEATURES"] = {
            "ENABLE_INSTRUCTOR_BACKGROUND_TASKS": True,
            "MAX_PROBLEM_RESPONSES_COUNT": 10000,
        }

    lms_general_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-lms-general-config-{stack_info.env_suffix}",
        metadata={
            "name": lms_general_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "81-lms-general-config.yaml": render_yaml(lms_general_config_content),
        },
    )

    # LMS interpolated configuration
    lms_interpolated_config_name = "82-lms-interpolated-config-yaml"
    lms_interpolated_config = {
        "APPZI_URL": edxapp_config.get("appzi_url", ""),
        "SITE_NAME": edxapp_config.require_object("domains")["lms"],
        "SESSION_COOKIE_NAME": f"{env_name}-edx-lms-sessionid",
        "MIT_LEARN_SUPPORT_SITE_LINK": f"mailto:{edxapp_config.require('sender_email_address')}",
    }

    lms_interpolated_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-lms-interpolated-config-{stack_info.env_suffix}",
        metadata={
            "name": lms_interpolated_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "82-lms-interpolated-config.yaml": render_yaml(lms_interpolated_config),
        },
        opts=ResourceOptions(delete_before_replace=True),
    )

    # UWsgi configuration (unchanged, read from file)
    uwsgi_ini_config_name = "uwsgi-ini"

    uwsgi_ini_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-uwsgi-ini-config-{stack_info.env_suffix}",
        metadata={
            "name": uwsgi_ini_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={"uwsgi.ini": Path("files/edxapp/uwsgi.ini").read_text()},
    )

    # Waffle flags configuration (unchanged, built from config)
    waffle_flags_yaml_config_name = "waffle-flags-yaml"
    waffle_list = edxapp_config.get_object("waffle_flags", default=[])
    waffle_flags_yaml_content = yaml.safe_dump({"waffles": waffle_list})

    waffle_flags_yaml_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-waffle-flags-{stack_info.env_suffix}",
        metadata={
            "name": waffle_flags_yaml_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "waffle-flags.yaml": waffle_flags_yaml_content,
        },
    )

    return EdxappConfigMaps(
        general=general_config_map,
        interpolated=interpolated_config_map,
        cms_general=cms_general_config_map,
        cms_interpolated=cms_interpolated_config_map,
        lms_general=lms_general_config_map,
        lms_interpolated=lms_interpolated_config_map,
        uwsgi_ini=uwsgi_ini_config_map,
        waffle_flags_yaml=waffle_flags_yaml_config_map,
        general_config_name=general_config_name,
        interpolated_config_name=interpolated_config_name,
        cms_general_config_name=cms_general_config_name,
        cms_interpolated_config_name=cms_interpolated_config_name,
        lms_general_config_name=lms_general_config_name,
        lms_interpolated_config_name=lms_interpolated_config_name,
        uwsgi_ini_config_name=uwsgi_ini_config_name,
        waffle_flags_yaml_config_name=waffle_flags_yaml_config_name,
    )
