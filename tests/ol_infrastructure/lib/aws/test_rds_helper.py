"""The blue/green path disables deletion protection with a live ModifyDBInstance call.

A `pulumi preview` that makes that call is a privileged write against the target
database, and it fails outright wherever the worker role lacks rds:ModifyDBInstance.
Under the preview-gated Concourse topology the preview is what opens the promotion gate,
so that failure blocks the environment from deploying at all.
"""

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
