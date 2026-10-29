"""Reconcile OpenMetadata's persisted authentication and authorizer settings.

OM 2.0 reads its authentication and authorizer configuration from the rows of
``openmetadata_settings`` whose ``configtype`` is
``authenticationConfiguration`` and ``authorizerConfiguration``, not from the
YAML that the Helm values and ``extraEnvs`` render.
``SettingsCache.createDefaultConfiguration`` seeds each row from the YAML only
when the row is absent, and ``SecurityConfigurationManager.initialize`` reads
them back out of the database on every boot, so on an installation that
predates a setting the environment variable for that setting never binds.
``sessionExpiry`` and ``maxActiveSessionsPerUser`` are both new in 2.0 and the
row 1.13.x wrote carries neither, which leaves them at their schema defaults:
unset (falling through to the deprecated ``oidcConfiguration.sessionExpiry``)
and 5.  ``useRolesFromProvider`` was false when our authorizer row was seeded,
and CI's authentication row still carries ``clientType: public`` from before
the switch to confidential.

Writing the keys straight into the row is what OM's own 2.0.2 native migration
does to the authentication row for ``tokenValidity``.  ``jsonb ||`` merges at
the top level, so every other key is preserved.

Ordering matters as much as the write.  ``SecurityConfigurationManager``'s
state and ``SessionService``'s copy of it are snapshots taken during startup,
so a written value only takes effect on the next server start.  The Job that
runs this is a dependency of the Helm release for that reason, and the server
pods carry a hash of this Job's inputs as an annotation so that the release
rolls them whenever it changes.
"""

import json
import os
import sys
from typing import Any

import psycopg2
from psycopg2 import errors

#: Desired top-level keys per settings row, supplied by the Pulumi stack so the
#: values live in one place alongside the Helm values and ``extraEnvs`` that
#: cover a from-scratch install.
DESIRED: dict[str, dict[str, Any]] = {
    "authenticationConfiguration": {
        "sessionExpiry": int(os.environ["OM_SESSION_EXPIRY_SECONDS"]),
        "maxActiveSessionsPerUser": int(os.environ["OM_MAX_ACTIVE_SESSIONS_PER_USER"]),
        "clientType": os.environ["OM_CLIENT_TYPE"],
    },
    "authorizerConfiguration": {
        "useRolesFromProvider": json.loads(os.environ["OM_USE_ROLES_FROM_PROVIDER"]),
    },
}

_SELECT_SQL = "SELECT json FROM openmetadata_settings WHERE configtype = %s"
_UPDATE_SQL = (
    "UPDATE openmetadata_settings SET json = json || %s::jsonb WHERE configtype = %s"
)


def _reconcile_row(cursor, config_type: str, desired: dict[str, Any]) -> None:
    """Add any missing or stale desired key to one stored settings row.

    :param cursor: An open cursor on the OpenMetadata database.
    :param config_type: The row's ``configtype``.
    :param desired: The top-level keys and values the row must carry.
    """
    cursor.execute(_SELECT_SQL, (config_type,))
    row = cursor.fetchone()
    if row is None:
        print(f"[ok]   no {config_type} row yet, skipping")  # noqa: T201
        return

    stored = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    drift = {key: value for key, value in desired.items() if stored.get(key) != value}
    if not drift:
        print(f"[ok]   {config_type} already at {desired}")  # noqa: T201
        return

    changed = ", ".join(
        f"{key}: {stored.get(key)!r} -> {value!r}"
        for key, value in sorted(drift.items())
    )
    cursor.execute(_UPDATE_SQL, (json.dumps(desired), config_type))
    print(f"[done] {config_type} updated: {changed}")  # noqa: T201


def main() -> None:
    """Reconcile every row in ``DESIRED``.

    :raises psycopg2.Error: On any connection or statement failure other than
        the settings table not existing yet.
    """
    connection = psycopg2.connect(
        host=os.environ["OM_DB_HOST"],
        port=os.environ["OM_DB_PORT"],
        dbname=os.environ["OM_DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_USER_PASSWORD"],
        sslmode="require",
    )
    try:
        with connection.cursor() as cursor:
            try:
                for config_type, desired in DESIRED.items():
                    _reconcile_row(cursor, config_type, desired)
            except errors.UndefinedTable:
                # First deploy of an environment: the server's migration init
                # container has not created the table yet.  The rows it goes on
                # to seed come from the YAML, which already carries every
                # value, so there is nothing for this to fix.
                connection.rollback()
                print("[ok]   no openmetadata_settings table yet, skipping")  # noqa: T201
                return
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[fail] {exc}", file=sys.stderr)  # noqa: T201
        sys.exit(1)
