"""Tests for the logging endpoints every Fastly service shares."""

from __future__ import annotations

import asyncio
import base64
from collections import Counter

import pulumi
import pytest

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


PROXY_LOGIN = ("fastly", "hunter2")


class StackReferenceMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.stack_references: list[str] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        outputs = dict(args.inputs)
        if args.typ == "pulumi:pulumi:StackReference":
            self.stack_references.append(args.name)
            outputs["outputs"] = {
                "vector_log_proxy_domain": "log-proxy.example.com",
                "fastly_access_logging_bucket": {"bucket_name": "access-logs"},
                "fastly_access_logging_iam_role": {"role_arn": "arn:aws:iam::1:role/f"},
            }
        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


mocks = StackReferenceMocks()
pulumi.runtime.set_mocks(mocks, project="project", stack="QA")

from ol_infrastructure.lib import fastly_logging  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _stack_reference_mocks():
    """Every test module installs its own mocks at import, and the last one
    collected wins, so reinstall these before the tests here run.
    """
    pulumi.runtime.set_mocks(mocks, project="project", stack="QA")
    fastly_logging._log_proxy.cache_clear()
    fastly_logging._log_archive.cache_clear()


@pytest.fixture(autouse=True)
def _proxy_credentials(monkeypatch):
    credentials = dict(zip(("username", "password"), PROXY_LOGIN, strict=True))
    monkeypatch.setattr(
        fastly_logging, "read_yaml_secrets", lambda _path: {"fastly": credentials}
    )


def _args():
    return fastly_logging.fastly_logging_args(
        name="fastly-app-qa",
        application="app",
        environment="qa",
        s3_path="/app/qa/",
    )


@pulumi.runtime.test
def test_https_endpoint_posts_to_the_log_proxy():
    endpoint = _args()["logging_https"][0]

    def check(args):
        url, header_value = args
        assert url == "https://log-proxy.example.com/fastly"
        expected = base64.b64encode(":".join(PROXY_LOGIN).encode()).decode()
        assert header_value == f"Basic {expected}"

    assert endpoint.name == "fastly-app-qa-https-logging-args"
    assert endpoint.content_type == "application/json"
    assert endpoint.format_version == 2
    assert endpoint.header_name == "Authorization"
    assert endpoint.json_format == "0"
    assert endpoint.method == "POST"
    assert endpoint.request_max_bytes == 1048576
    return pulumi.Output.all(endpoint.url, endpoint.header_value).apply(check)


@pulumi.runtime.test
def test_proxy_credentials_are_secret():
    endpoint = _args()["logging_https"][0]

    def check(is_secret):
        assert is_secret

    return pulumi.Output.from_input(endpoint.header_value.is_secret()).apply(check)


@pulumi.runtime.test
def test_https_records_carry_the_loki_labels():
    """The proxy labels on these two fields and cannot label a record without them."""
    log_format = _args()["logging_https"][0].format
    # The order is pinned as well. A reordered format string is a new version of
    # every service that already sent these fields.
    assert log_format.startswith('{"environment":"qa","application":"app","client_ip"')


@pulumi.runtime.test
def test_s3_endpoint_archives_under_the_given_path():
    endpoint = _args()["logging_s3s"][0]

    def check(args):
        bucket_name, s3_iam_role = args
        assert bucket_name == "access-logs"
        assert s3_iam_role == "arn:aws:iam::1:role/f"

    assert endpoint.name == "fastly-app-qa-s3-logging-args"
    assert endpoint.gzip_level == 3
    assert endpoint.message_type == "blank"
    assert endpoint.path == "/app/qa/"
    assert endpoint.redundancy == "standard"
    assert '"application"' not in endpoint.format
    return pulumi.Output.all(endpoint.bucket_name, endpoint.s3_iam_role).apply(check)


@pulumi.runtime.test
def test_a_second_service_reuses_the_stack_references():
    """OCW declares three services in one stack; a repeated reference is a
    duplicate URN.
    """
    fastly_logging._log_proxy.cache_clear()
    fastly_logging._log_archive.cache_clear()
    mocks.stack_references.clear()
    services = [_args(), _args()]

    def check(_):
        assert Counter(mocks.stack_references) == {
            "organization/ol-infrastructure-vector-log-proxy/operations.QA": 1,
            "organization/ol-infrastructure-monitoring/default": 1,
        }

    # Wait on the outputs of both services so every reference either of them
    # declared has registered before counting.
    return pulumi.Output.all(
        *(
            output
            for service in services
            for output in (
                service["logging_https"][0].url,
                service["logging_s3s"][0].bucket_name,
            )
        )
    ).apply(check)
