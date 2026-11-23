"""Shared setup for OLAmazonDB tests: live-lookup stubs and a base Postgres config."""

import pytest

from ol_infrastructure.components.aws import database
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.lib.aws import rds_helper


def stub_rds_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the AWS API lookups OLAmazonDB makes with fixed answers."""
    # Both modules hold their own reference: the config validators use
    # database's, max_minor_version uses rds_helper's.
    for module in (database, rds_helper):
        monkeypatch.setattr(module, "db_engines", lambda: {"postgres": ["16.15"]})
    monkeypatch.setattr(database, "get_rds_instance", lambda _name: {})
    monkeypatch.setattr(
        database, "parameter_group_family", lambda _engine, _version: "postgres16"
    )


def postgres_db(name: str, **overrides) -> OLAmazonDB:
    """Build an OLAmazonDB from a minimal Postgres config plus ``overrides``."""
    config = OLPostgresDBConfig(
        instance_name=name,
        password="not-a-real-password",  # pragma: allowlist secret
        subnet_group_name="test-subnets",
        security_groups=[],
        tags={"OU": "operations", "Environment": "test"},
        db_name="testdb",
        engine_major_version="16",
        **overrides,
    )
    return OLAmazonDB(config)
