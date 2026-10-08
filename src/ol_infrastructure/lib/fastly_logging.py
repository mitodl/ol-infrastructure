"""Log delivery settings shared by every Fastly service.

This lives apart from :mod:`ol_infrastructure.lib.fastly` because it reads the
vector log proxy credentials. The Concourse secrets map follows imports, so a
project that imports this module has to watch ``vector/``, and a service that
does not ship logs should not pick that up by importing the provider helper.
"""

import base64
from functools import cache
from pathlib import Path
from typing import TypedDict

import pulumi
import pulumi_fastly as fastly

from bridge.lib.magic_numbers import ONE_MEGABYTE_BYTE
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.fastly import build_fastly_log_format_string
from ol_infrastructure.lib.pulumi_helper import make_stack_reference, parse_stack

S3_LOG_GZIP_LEVEL = 3


class FastlyLoggingArgs(TypedDict):
    """The logging arguments of :class:`pulumi_fastly.ServiceVcl`."""

    logging_https: list[fastly.ServiceVclLoggingHttpArgs]
    logging_s3s: list[fastly.ServiceVclLoggingS3Args]


# Cached because a stack can declare more than one service (OCW has three) and a
# second StackReference with the same name is a duplicate URN.
@cache
def _log_proxy() -> tuple[pulumi.Output[str], pulumi.Output[str]]:
    """Look up where the vector log proxy listens and how to authenticate to it.

    :returns: The URL that Fastly posts to and the Authorization header value.
    :rtype: tuple[pulumi.Output[str], pulumi.Output[str]]
    """
    stack_info = parse_stack()
    log_proxy_stack = make_stack_reference(
        projects.VECTOR_LOG_PROXY, f"operations.{stack_info.name}"
    )
    credentials = read_yaml_secrets(
        Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
    )["fastly"]
    encoded_credentials = base64.b64encode(
        f"{credentials['username']}:{credentials['password']}".encode()
    ).decode()
    return (
        log_proxy_stack.require_output("vector_log_proxy_domain").apply(
            lambda domain: f"https://{domain}/fastly"
        ),
        pulumi.Output.secret(f"Basic {encoded_credentials}"),
    )


@cache
def _log_archive() -> tuple[pulumi.Output[str], pulumi.Output[str]]:
    """Look up the S3 bucket that holds Fastly access logs.

    :returns: The bucket name and the ARN of the role Fastly assumes to write to it.
    :rtype: tuple[pulumi.Output[str], pulumi.Output[str]]
    """
    monitoring_stack = make_stack_reference(projects.MONITORING, "default")
    return (
        monitoring_stack.require_output("fastly_access_logging_bucket")["bucket_name"],
        monitoring_stack.require_output("fastly_access_logging_iam_role")["role_arn"],
    )


def fastly_logging_args(
    *,
    name: str,
    application: str,
    environment: str,
    s3_path: str,
) -> FastlyLoggingArgs:
    """Build the logging endpoints for a Fastly service.

    Every service posts its access logs to the vector log proxy, which forwards
    them to Grafana Cloud, and archives the same records to S3.

    :param name: Prefix for the endpoint names, unique within the service.
    :param application: Value of the ``application`` label in Loki. The proxy
        cannot label a record that does not carry it.
    :param environment: Value of the ``environment`` label in Loki.
    :param s3_path: Key prefix for the archived logs, with a leading and a
        trailing slash. Changing it for a live service splits its archive.
    :returns: Keyword arguments to splat into :class:`pulumi_fastly.ServiceVcl`.
    :rtype: FastlyLoggingArgs
    """
    log_proxy_url, log_proxy_authorization = _log_proxy()
    bucket_name, s3_iam_role = _log_archive()
    return {
        "logging_https": [
            fastly.ServiceVclLoggingHttpArgs(
                url=log_proxy_url,
                name=f"{name}-https-logging-args",
                content_type="application/json",
                format=build_fastly_log_format_string(
                    additional_static_fields={
                        "application": application,
                        "environment": environment,
                    }
                ),
                format_version=2,
                header_name="Authorization",
                header_value=log_proxy_authorization,
                json_format="0",
                method="POST",
                request_max_bytes=ONE_MEGABYTE_BYTE,
            )
        ],
        "logging_s3s": [
            fastly.ServiceVclLoggingS3Args(
                bucket_name=bucket_name,
                name=f"{name}-s3-logging-args",
                format=build_fastly_log_format_string(additional_static_fields={}),
                gzip_level=S3_LOG_GZIP_LEVEL,
                message_type="blank",
                path=s3_path,
                redundancy="standard",
                s3_iam_role=s3_iam_role,
            )
        ],
    }
