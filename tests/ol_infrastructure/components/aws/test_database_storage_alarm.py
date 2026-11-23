"""Tests for OLAmazonDB's FreeStorageSpace alarm.

The threshold scales with the configured storage so that it fires while there is
still room to act on a large volume, and the read replica gets its own copy because
it fills independently of the primary.
"""

import asyncio

import pulumi
import pytest

from ol_infrastructure.components.aws import database
from ol_infrastructure.components.aws.database import (
    OLAmazonDB,
    OLPostgresDBConfig,
    OLReplicaDBConfig,
)
from ol_infrastructure.lib.aws import rds_helper
from tests.ol_infrastructure.recording_mocks import RecordingMocks

ONE_GIBIBYTE_BYTES = 2**30

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def alarm_configs(monkeypatch) -> list[database.OLCloudWatchAlarmSimpleRDSConfig]:
    """Stub the live RDS lookups and capture every alarm config OLAmazonDB builds."""
    for module in (database, rds_helper):
        monkeypatch.setattr(module, "db_engines", lambda: {"postgres": ["16.15"]})
    monkeypatch.setattr(database, "get_rds_instance", lambda _name: {})
    monkeypatch.setattr(
        database, "parameter_group_family", lambda _engine, _version: "postgres16"
    )
    captured: list[database.OLCloudWatchAlarmSimpleRDSConfig] = []

    def capture(alarm_config: database.OLCloudWatchAlarmSimpleRDSConfig) -> None:
        captured.append(alarm_config)

    monkeypatch.setattr(database, "OLCloudWatchAlarmSimpleRDS", capture)
    pulumi.runtime.set_mocks(RecordingMocks())
    return captured


def _db(name: str, **overrides) -> OLAmazonDB:
    overrides.setdefault("monitoring_profile_name", "production")
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


def _free_storage_alarms(alarm_configs, identifier: str):
    return [
        alarm
        for alarm in alarm_configs
        if alarm.metric_name == "FreeStorageSpace"
        and alarm.database_identifier == identifier
    ]


@pytest.mark.parametrize(
    ("storage_gb", "expected_threshold_gib"),
    [
        (50, 5),  # 5% is 2.5 GiB, so the 5 GiB floor applies
        (100, 5),
        (2000, 100),
    ],
)
@pulumi.runtime.test
def test_threshold_is_five_percent_of_storage_with_a_floor(
    alarm_configs, storage_gb, expected_threshold_gib
):
    _db(f"storage-{storage_gb}-db", storage=storage_gb)

    (alarm,) = _free_storage_alarms(alarm_configs, f"storage-{storage_gb}-db")
    assert alarm.threshold == expected_threshold_gib * ONE_GIBIBYTE_BYTES


@pulumi.runtime.test
def test_replica_gets_only_a_storage_alarm(alarm_configs):
    _db(
        "replicated-db",
        storage=2000,
        read_replica=OLReplicaDBConfig(),
    )

    replica_alarms = [
        alarm
        for alarm in alarm_configs
        if alarm.database_identifier == "replicated-db-replica"
    ]
    assert [alarm.metric_name for alarm in replica_alarms] == ["FreeStorageSpace"]
    assert replica_alarms[0].threshold == 100 * ONE_GIBIBYTE_BYTES


@pulumi.runtime.test
def test_disabled_profile_creates_no_alarms(alarm_configs):
    _db(
        "unmonitored-db",
        monitoring_profile_name="disabled",
        read_replica=OLReplicaDBConfig(),
    )

    assert alarm_configs == []
