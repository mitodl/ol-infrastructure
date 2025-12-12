# ruff: noqa: E501, PLR0913, FBT003
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pulumi_kubernetes as kubernetes
import yaml
from pulumi import Config, Output, ResourceOptions, StackReference

from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.lib.pulumi_helper import StackInfo


def _build_interpolated_config_template(
    stack_info: StackInfo,
    edxapp_config: Config,
    runtime_config: dict[str, str],
    storage_bucket_name: str,
    course_bucket_name: str,
    grades_bucket_name: str,
    ses_configuration_set: str,
    env_name: str,
) -> str:
    """Build the interpolated config YAML template with conditional sections.

    This function generates the 60-interpolated-config.yaml template, extracting
    complex conditionals out of nested f-strings for better readability.
    """
    template_parts = [
        f"""
        ALLOWED_HOSTS:
        - {edxapp_config.require_object("domains")["lms"]}
        - {edxapp_config.require_object("domains")["preview"]}
        - {edxapp_config.require_object("domains")["studio"]}
        - {edxapp_config.require("backend_lms_domain")}
        - {edxapp_config.require("backend_studio_domain")}
        - {edxapp_config.require("backend_preview_domain")}
        # TODO: Remove after Django 5.2 migration - replaced by STORAGES configuration
        AWS_S3_CUSTOM_DOMAIN: {storage_bucket_name}.s3.amazonaws.com
        # TODO: Remove after Django 5.2 migration - replaced by STORAGES configuration
        AWS_STORAGE_BUCKET_NAME: {storage_bucket_name}
        AWS_SES_CONFIGURATION_SET: {ses_configuration_set}
        BASE_COOKIE_DOMAIN: {edxapp_config.require_object("domains")["lms"]}
        BLOCK_STRUCTURES_SETTINGS:
          COURSE_PUBLISH_TASK_DELAY: 30
          PRUNING_ACTIVE: true  # MODIFIED
          TASK_DEFAULT_RETRY_DELAY: 30
          TASK_MAX_RETRIES: 5
          # TODO: Remove after Django 5.2 migration - replaced by STORAGES configuration
          STORAGE_CLASS: storages.backends.s3boto3.S3Boto3Storage
          DIRECTORY_PREFIX: coursestructure/
          # TODO: Remove after Django 5.2 migration - replaced by STORAGES configuration
          STORAGE_KWARGS:
            bucket_name: {storage_bucket_name}
            default_acl: public-read
        EMAIL_USE_COURSE_ID_FROM_FOR_BULK: {edxapp_config.get_bool("email_use_course_id_from_for_bulk", False)}
        BULK_EMAIL_DEFAULT_FROM_EMAIL: {edxapp_config.get("bulk_email_default_from_email") or edxapp_config.require("sender_email_address")}
        CELERY_BROKER_HOSTNAME: {runtime_config["redis_hostname"]}
        CMS_BASE: {edxapp_config.require_object("domains")["studio"]}
        CONTACT_EMAIL: {edxapp_config.require("sender_email_address")}
        CORS_ORIGIN_WHITELIST:
        - https://{edxapp_config.require_object("domains")["lms"]}
        - https://{edxapp_config.require_object("domains")["studio"]}
        - https://{edxapp_config.require_object("domains")["preview"]}
        - https://{edxapp_config.require("marketing_domain")}
        - https://{runtime_config["notes_domain"]}
        - https://{edxapp_config.require("learn_ai_frontend_domain")}
        - https://{stack_info.env_prefix}-{stack_info.env_suffix}-edxapp-storage.s3.amazonaws.com  # Fix ORA upload bug
        - https://idp.mit.edu  # For residential login
        COURSE_IMPORT_EXPORT_BUCKET: {course_bucket_name}
        CROSS_DOMAIN_CSRF_COOKIE_DOMAIN: {edxapp_config.require_object("domains")["lms"]}
        CROSS_DOMAIN_CSRF_COOKIE_NAME: {env_name}-edxapp-csrftoken
        DEFAULT_FEEDBACK_EMAIL: {edxapp_config.require("sender_email_address")}
        DEFAULT_FROM_EMAIL: {edxapp_config.require("sender_email_address")}
        DISCUSSIONS_MICROFRONTEND_URL: https://{edxapp_config.require_object("domains")["lms"]}/discuss
        EDXMKTG_USER_INFO_COOKIE_NAME: {env_name}-edx-user-info
        EDXNOTES_INTERNAL_API: https://{runtime_config["notes_domain"]}/api/v1
        EDXNOTES_PUBLIC_API: https://{runtime_config["notes_domain"]}/api/v1
        ELASTIC_SEARCH_CONFIG:
        - host: {runtime_config["opensearch_hostname"]}
          port: 443
          use_ssl: true
        ELASTIC_SEARCH_CONFIG_ES7:
        - host: {runtime_config["opensearch_hostname"]}
          port: 443
          use_ssl: true
        # TODO: Remove after Django 5.2 migration - replaced by STORAGES configuration
        FILE_UPLOAD_STORAGE_BUCKET_NAME: {storage_bucket_name}
        FORUM_ELASTIC_SEARCH_CONFIG:
        - host: {runtime_config["opensearch_hostname"]}
          port: "443"
          use_ssl: true
        FORUM_MONGODB_DATABASE: "forum"
        GITHUB_REPO_ROOT: /openedx/data
        GOOGLE_ANALYTICS_ACCOUNT: {edxapp_config.require("google_analytics_id")}
        GRADES_DOWNLOAD:
          BUCKET: {grades_bucket_name}  # MODIFIED
          ROOT_PATH: grades  # MODIFIED
          # TODO: Remove after Django 5.2 migration - replaced by STORAGES configuration
          STORAGE_CLASS: django.core.files.storage.S3Storage  # MODIFIED
          # TODO: Remove after Django 5.2 migration - replaced by STORAGES configuration
          STORAGE_KWARGS:
            location: grades/
          # TODO: Remove after Django 5.2 migration - replaced by STORAGES configuration
          STORAGE_TYPE: S3  # MODIFIED
        IDA_LOGOUT_URI_LIST:
        - https://{edxapp_config.require("marketing_domain")}/logout
        - https://{edxapp_config.require_object("domains")["studio"]}/logout
        - https://{edxapp_config.require("mit_learn_api_domain")}/logout
        LANGUAGE_COOKIE: {env_name}-openedx-language-preference
        MIT_LEARN_AI_API_URL: https://{edxapp_config.require("mit_learn_api_domain")}/ai  # Added for ol_openedx_chat
        MIT_LEARN_API_BASE_URL: https://{edxapp_config.require("mit_learn_api_domain")}/learn  # Added for ol_openedx_chat
        MIT_LEARN_SUMMARY_FLASHCARD_URL: https://{edxapp_config.require("mit_learn_api_domain")}/learn/api/v1/contentfiles/  # Added for ol_openedx_chat
        MIT_LEARN_BASE_URL: "https://{edxapp_config.require("mit_learn_domain")}"
        MIT_LEARN_AI_XBLOCK_CHAT_API_URL: https://{edxapp_config.require("mit_learn_api_domain")}/ai/http/canvas_syllabus_agent/
        MIT_LEARN_AI_XBLOCK_TUTOR_CHAT_API_URL:  https://{edxapp_config.require("mit_learn_api_domain")}/ai/http/canvas_tutor_agent/
        MIT_LEARN_AI_XBLOCK_PROBLEM_SET_LIST_URL: https://{edxapp_config.require("mit_learn_api_domain")}/ai/api/v0/problem_set_list  # Added for ol_openedx_chat_xblock
        MIT_LEARN_AI_XBLOCK_CHAT_RATING_URL: https://{edxapp_config.require("mit_learn_api_domain")}/ai/api/v0/chat_sessions/  # Added for ol_openedx_chat_xblock
        MIT_LEARN_LOGO: https://{edxapp_config.require_object("domains")["lms"]}/static/mitxonline/images/mit-learn-logo.svg
        LEARNING_MICROFRONTEND_URL: https://{edxapp_config.require_object("domains")["lms"]}/learn
        LMS_BASE: {edxapp_config.require_object("domains")["lms"]}
        LMS_INTERNAL_ROOT_URL: https://{edxapp_config.require_object("domains")["lms"]}
        LMS_ROOT_URL: https://{edxapp_config.require_object("domains")["lms"]}
        LOGIN_REDIRECT_WHITELIST:  # MODIFIED
        - {edxapp_config.require_object("domains")["studio"]}
        - {edxapp_config.require_object("domains")["lms"]}
        - {edxapp_config.require_object("domains")["preview"]}
        - {edxapp_config.require("marketing_domain")}
        LOGO_URL: https://{edxapp_config.require_object("domains")["lms"]}/static/{stack_info.env_prefix}/images/logo.svg
        LOGO_URL_PNG_FOR_EMAIL: https://{edxapp_config.require_object("domains")["lms"]}/static/{stack_info.env_prefix}/images/logo.png
        LOGO_TRADEMARK_URL: https://{edxapp_config.require_object("domains")["lms"]}/static/{stack_info.env_prefix}/images/{"mit-ol-logo" if stack_info.env_prefix == "xpro" else "mit-logo"}.svg
        MARKETING_SITE_BASE_URL: https://{edxapp_config.require("marketing_domain")}/ # ADDED - to support mitxonline-theme
        MARKETING_SITE_CHECKOUT_URL: https://{edxapp_config.get("marketing_domain") or edxapp_config.get("marketing_domain")}/cart/add/ # ADDED - to support mitxonline checkout
        MKTG_URLS:
          ROOT: https://{edxapp_config.require("marketing_domain")}/
        MKTG_URL_OVERRIDES:
          COURSES: https://{edxapp_config.require("marketing_domain")}/{"catalog/" if stack_info.env_prefix == "xpro" else ""}
          PRIVACY: https://{edxapp_config.require("marketing_domain")}/privacy{"-policy/" if stack_info.env_prefix == "xpro" else ""}
          TOS: https://{edxapp_config.require("marketing_domain")}/terms{"-of-service/" if stack_info.env_prefix == "xpro" else ""}
          ABOUT: https://{edxapp_config.require("marketing_domain")}/about{"-us" if stack_info.env_prefix == "xpro" else ""}
          HONOR: https://{edxapp_config.require("marketing_domain")}/honor-code/
          ACCESSIBILITY: https://accessibility.mit.edu/
          CONTACT: https://{stack_info.env_prefix}.zendesk.com/hc/en-us/requests/new/
          TOS_AND_HONOR: ''
        NOTIFICATIONS_DEFAULT_FROM_EMAIL: {edxapp_config.get("bulk_email_default_from_email") or edxapp_config.require("sender_email_address")}
        PAYMENT_SUPPORT_EMAIL: {edxapp_config.require("sender_email_address")}
        PREVIEW_LMS_BASE: {edxapp_config.require_object("domains")["preview"]}
        SENTRY_ENVIRONMENT: {env_name}
        # Removing the session cookie domain as it is no longer needed for sharing the cookie
        # between LMS and Studio (TMM 2021-10-22)
        # UPDATE: The session cookie domain appears to still be required for enabling the
        # preview subdomain to share authentication with LMS (TMM 2021-12-20)
        SESSION_COOKIE_DOMAIN: {".{}".format(edxapp_config.require_object("domains")["lms"].split(".", 1)[-1])}
        UNIVERSITY_EMAIL: {edxapp_config.require("sender_email_address")}
        # OpenTelemetry configuration
        OPENEDX_TELEMETRY:
        - edx_django_utils.monitoring.OpenTelemetryBackend
        OTEL_EXPORTER_OTLP_ENDPOINT: http://grafana-k8s-monitoring-alloy-receiver.grafana.svc.cluster.local:4318
        OTEL_SERVICE_NAME: {env_name}-edxapp
        OTEL_LOG_LEVEL: info
        ECOMMERCE_PUBLIC_URL_ROOT: {edxapp_config.require_object("domains")["lms"]}"""
    ]

    # Special handling of CSRF_TRUSTED_ORIGINS for residential
    if stack_info.env_prefix in ["mitx", "mitx-staging"]:
        template_parts.append(
            f"""
        CSRF_TRUSTED_ORIGINS:  # MODIFIED
        - https://canvas.mit.edu
        - https://{edxapp_config.require_object("domains")["lms"]}"""
        )
    else:
        template_parts.append(
            f"""
        CSRF_TRUSTED_ORIGINS:  # MODIFIED
        - https://{edxapp_config.require_object("domains")["lms"]}"""
        )

    # Special configuration unique to residential
    if stack_info.env_prefix in ["mitx", "mitx-staging"]:
        template_parts.append(
            f"""
        CANVAS_BASE_URL: {edxapp_config.require("canvas_base_url")}
        """
        )

    # Configuration unique to xPro
    if stack_info.env_prefix == "xpro":
        template_parts.append(
            f"""
        XPRO_BASE_URL: https://{edxapp_config.require("marketing_domain")}"""
        )

    return textwrap.dedent("".join(template_parts))


@dataclass
class EdxappConfigMaps:
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
    """Create all the k8s configmaps needed for edxapp."""
    general_config_name = "50-general-config-yaml"
    general_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-general-config-{stack_info.env_suffix}",
        metadata={
            "name": general_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "50-general-config.yaml": Path(
                f"files/edxapp/{stack_info.env_prefix}/50-general-config.yaml"
            ).read_text()
        },
    )

    # Misc values needed for the next step
    env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
    course_bucket_name = f"{env_name}-edxapp-courses"
    grades_bucket_name = f"{env_name}-edxapp-grades"
    storage_bucket_name = f"{env_name}-edxapp-storage"
    ses_configuration_set = f"edxapp-{env_name}"

    # Load environment specific configuration directly from code into a configmap
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
                "60-interpolated-config.yaml": _build_interpolated_config_template(
                    stack_info=stack_info,
                    edxapp_config=edxapp_config,
                    runtime_config=runtime_config,
                    storage_bucket_name=storage_bucket_name,
                    course_bucket_name=course_bucket_name,
                    grades_bucket_name=grades_bucket_name,
                    ses_configuration_set=ses_configuration_set,
                    env_name=env_name,
                ),
            },
            opts=ResourceOptions(delete_before_replace=True),
        )
    )

    # General configuration items for the CMS application.
    cms_general_config_name = "71-cms-general-config-yaml"
    cms_general_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-cms-general-config-{stack_info.env_suffix}",
        metadata={
            "name": cms_general_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "71-cms-general-config.yaml": Path(
                f"files/edxapp/{stack_info.env_prefix}/71-cms-general-config.yaml"
            ).read_text()
        },
    )

    # Interpolated configuration items for the CMS application.
    cms_interpolated_config_name = "72-cms-interpolated-config-yaml"
    cms_interpolated_config_content = textwrap.dedent(f"""
        SITE_NAME: {edxapp_config.require_object("domains")["studio"]}
        SOCIAL_AUTH_EDX_OAUTH2_URL_ROOT: https://{edxapp_config.require_object("domains")["lms"]}
        SOCIAL_AUTH_EDX_OAUTH2_PUBLIC_URL_ROOT: https://{edxapp_config.require_object("domains")["lms"]}
        SESSION_COOKIE_NAME: {env_name}-edx-studio-sessionid
    """)
    if stack_info.env_prefix == "mitxonline":
        cms_interpolated_config_content += (
            f"GITHUB_ORG_API_URL: {edxapp_config.require('github_org_api_url')}\n"
        )

    cms_interpolated_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-cms-interpolated-config-{stack_info.env_suffix}",
        metadata={
            "name": cms_interpolated_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={"72-cms-interpolated-config.yaml": cms_interpolated_config_content},
        opts=ResourceOptions(delete_before_replace=True),
    )

    lms_general_config_name = "81-lms-general-config-yaml"
    lms_general_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-lms-general-config-{stack_info.env_suffix}",
        metadata={
            "name": lms_general_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "81-lms-general-config.yaml": Path(
                f"files/edxapp/{stack_info.env_prefix}/81-lms-general-config.yaml"
            ).read_text()
        },
    )

    # Interpolated configuration items for the CMS application.
    lms_interpolated_config_name = "82-lms-interpolated-config-yaml"
    lms_interpolated_config_map = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-lms-interpolated-config-{stack_info.env_suffix}",
        metadata={
            "name": lms_interpolated_config_name,
            "namespace": namespace,
            "labels": k8s_global_labels,
        },
        data={
            "82-lms-interpolated-config.yaml": textwrap.dedent(f"""
                APPZI_URL: {edxapp_config.get("appzi_url", "")}
                SITE_NAME: {edxapp_config.require_object("domains")["lms"]}
                SESSION_COOKIE_NAME: {env_name}-edx-lms-sessionid
                MIT_LEARN_SUPPORT_SITE_LINK: mailto:{edxapp_config.require("sender_email_address")}
            """)
        },
        opts=ResourceOptions(delete_before_replace=True),
    )

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
