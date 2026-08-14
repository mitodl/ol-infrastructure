"""The blue/green path disables deletion protection with a live ModifyDBInstance call.

A `pulumi preview` that makes that call is a privileged write against the target
database, and it fails outright wherever the worker role lacks rds:ModifyDBInstance.
Under the preview-gated Concourse topology the preview is what opens the promotion gate,
so that failure blocks the environment from deploying at all.
"""

import re

import pytest

from ol_infrastructure.lib.aws import rds_helper


@pytest.fixture
def recorded_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rds_helper.rds_client,
        "modify_db_instance",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


def _set_dry_run(monkeypatch, *, dry_run: bool):
    monkeypatch.setattr(rds_helper.pulumi.runtime, "is_dry_run", lambda: dry_run)


def test_preview_makes_no_modify_call(recorded_calls, monkeypatch):
    _set_dry_run(monkeypatch, dry_run=True)
    rds_helper.turn_off_deletion_protection("edxapp-db-xpro-production")
    assert recorded_calls == []


def test_already_unprotected_makes_no_modify_call(recorded_calls, monkeypatch):
    _set_dry_run(monkeypatch, dry_run=False)
    rds_helper.turn_off_deletion_protection(
        "edxapp-db-xpro-production", currently_protected=False
    )
    assert recorded_calls == []


def test_update_disables_protection(recorded_calls, monkeypatch):
    _set_dry_run(monkeypatch, dry_run=False)
    rds_helper.turn_off_deletion_protection("edxapp-db-mitx-production")
    assert recorded_calls == [
        {
            "DBInstanceIdentifier": "edxapp-db-mitx-production",
            "ApplyImmediately": True,
            "DeletionProtection": False,
        }
    ]


class TestPostgresMaxConnections:
    """PgBouncer's ``max_db_connections`` for Dagster is derived from this.

    Overstating it silently overcommits the pool against the database -- the failure
    mode that took Dagster down on 2026-08-10 -- so the arithmetic is worth pinning.
    """

    @staticmethod
    def _stub_ec2(monkeypatch, size_mib: int | None):
        """Stand in for EC2 ``describe_instance_types``; ``None`` = unknown class."""
        instance_types = (
            [] if size_mib is None else [{"MemoryInfo": {"SizeInMiB": size_mib}}]
        )
        monkeypatch.setattr(
            rds_helper.ec2_client,
            "describe_instance_types",
            lambda **_: {"InstanceTypes": instance_types},
        )
        # The function is lru_cached, so each case needs a clean slate.
        rds_helper.postgres_max_connections.cache_clear()

    def test_below_the_cap_divides_instance_memory(self, monkeypatch):
        # db.m7g.large, 8 GiB: 8589934592 / 9531392 = 901
        self._stub_ec2(monkeypatch, 8192)
        assert rds_helper.postgres_max_connections("db.m7g.large") == 901

    def test_large_classes_are_held_at_the_cap(self, monkeypatch):
        # db.r7g.2xlarge, 64 GiB, computes 7210 before the LEAST(). Verified against
        # ol-etl-db-production, where SHOW max_connections returns 5000.
        self._stub_ec2(monkeypatch, 65536)
        assert rds_helper.postgres_max_connections("db.r7g.2xlarge") == 5000

    def test_the_db_prefix_is_stripped_for_ec2(self, monkeypatch):
        queried = {}

        def _record(**kwargs):
            queried.update(kwargs)
            return {"InstanceTypes": [{"MemoryInfo": {"SizeInMiB": 4096}}]}

        monkeypatch.setattr(rds_helper.ec2_client, "describe_instance_types", _record)
        rds_helper.postgres_max_connections.cache_clear()
        rds_helper.postgres_max_connections("db.t4g.medium")
        assert queried["InstanceTypes"] == ["t4g.medium"]

    def test_unknown_instance_class_names_the_bad_value(self, monkeypatch):
        self._stub_ec2(monkeypatch, None)
        with pytest.raises(ValueError, match=re.escape("db.nonexistent.xlarge")):
            rds_helper.postgres_max_connections("db.nonexistent.xlarge")
