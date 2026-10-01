"""Tests for where OLAmazonDB puts its Enhanced Monitoring role.

IAM cannot change a role's path in place, so the default must keep rendering no
path at all (the provider's "/"): rendering one would replace every monitoring
role already in use. A stack that opts in gets exactly the path it asked for.
"""

import asyncio

import pulumi
import pytest

from ol_infrastructure.components.aws import database
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig

ROLE_TYPE = "aws:iam/role:Role"

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class RecordingMocks(pulumi.runtime.Mocks):
    """Echo inputs back as outputs and remember every resource created."""

    def __init__(self):
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        return f"{args.name}_id", {**args.inputs, "arn": f"arn:{args.name}"}

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}, None


@pytest.fixture
def mocks(monkeypatch):
    """Stub the live RDS lookups and record what OLAmazonDB creates."""
    monkeypatch.setattr(database, "db_engines", lambda: {"postgres": ["16.15"]})
    monkeypatch.setattr(database, "get_rds_instance", lambda _name: {})
    monkeypatch.setattr(
        database, "parameter_group_family", lambda _engine, _version: "postgres16"
    )
    recorder = RecordingMocks()
    pulumi.runtime.set_mocks(recorder)
    return recorder


def _db(name: str, **overrides) -> OLAmazonDB:
    config = OLPostgresDBConfig(
        instance_name=name,
        password="not-a-real-password",  # pragma: allowlist secret
        subnet_group_name="test-subnets",
        security_groups=[],
        tags={"OU": "operations", "Environment": "test"},
        db_name="testdb",
        engine_major_version="16",
        enhanced_monitoring_interval=60,
        monitoring_profile_name="disabled",
        **overrides,
    )
    return OLAmazonDB(config)


def _monitoring_role(mocks: RecordingMocks, name: str):
    return next(
        args
        for args in mocks.resources
        if args.typ == ROLE_TYPE and args.name == f"{name}-enhanced-monitoring-role"
    )


@pulumi.runtime.test
def test_default_leaves_the_role_at_the_root_path(mocks):
    db = _db("default-path-db")

    def check(_):
        assert "path" not in _monitoring_role(mocks, "default-path-db").inputs

    return db.db_instance.id.apply(check)


@pulumi.runtime.test
def test_opted_in_path_is_rendered(mocks):
    db = _db("scoped-path-db", enhanced_monitoring_role_path="/ol-infrastructure/rds/")

    def check(_):
        role = _monitoring_role(mocks, "scoped-path-db")
        assert role.inputs["path"] == "/ol-infrastructure/rds/"

    return db.db_instance.id.apply(check)
