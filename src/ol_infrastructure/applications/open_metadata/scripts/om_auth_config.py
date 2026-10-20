"""Reconcile OpenMetadata's persisted authentication settings.

OM 2.0 reads its authentication configuration from the row of
``openmetadata_settings`` whose ``configtype`` is
``authenticationConfiguration``, not from the YAML that the Helm values and
``extraEnvs`` render.  ``SettingsCache.createDefaultConfiguration`` seeds that
row from the YAML only when the row is absent, and
``SecurityConfigurationManager.initialize`` reads it back out of the database
on every boot, so on an installation that predates a setting the environment
variable for that setting never binds.  ``sessionExpiry`` and
``maxActiveSessionsPerUser`` are both new in 2.0 and the row 1.13.x wrote
carries neither, which leaves them at their schema defaults: unset (falling
through to the deprecated ``oidcConfiguration.sessionExpiry``) and 5.

Writing the keys straight into the row is what OM's own 2.0.2 native migration
does to this same row for ``tokenValidity``.  ``jsonb ||`` merges at the top
level, so every other key is preserved.

Ordering matters as much as the write.  ``SecurityConfigurationManager``'s
state and ``SessionService``'s copy of it are snapshots taken during startup,
so a written value only takes effect on the next server start.  The Job that
runs this is a dependency of the Helm release for that reason: the release's
own rollout is what picks the new values up.
"""

import json
import os
import sys

import psycopg2
from psycopg2 import errors

#: Desired top-level keys, supplied by the Pulumi stack so the values live in
#: one place alongside the ``extraEnvs`` that cover a from-scratch install.
DESIRED: dict[str, int] = {
    "sessionExpiry": int(os.environ["OM_SESSION_EXPIRY_SECONDS"]),
    "maxActiveSessionsPerUser": int(os.environ["OM_MAX_ACTIVE_SESSIONS_PER_USER"]),
}

_SELECT_SQL = (
    "SELECT json FROM openmetadata_settings "
    "WHERE configtype = 'authenticationConfiguration'"
)
_UPDATE_SQL = (
    "UPDATE openmetadata_settings SET json = json || %s::jsonb "
    "WHERE configtype = 'authenticationConfiguration'"
)


def main() -> None:
    """Add any missing or stale desired key to the stored auth configuration.

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
                cursor.execute(_SELECT_SQL)
            except errors.UndefinedTable:
                # First deploy of an environment: the server's migration init
                # container has not created the table yet.  The row it goes on
                # to seed comes from the YAML, which already carries both
                # values, so there is nothing for this to fix.
                connection.rollback()
                print("[ok]   no openmetadata_settings table yet, skipping")  # noqa: T201
                return

            row = cursor.fetchone()
            if row is None:
                print("[ok]   no authenticationConfiguration row yet, skipping")  # noqa: T201
                return

            stored = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            drift = {
                key: value for key, value in DESIRED.items() if stored.get(key) != value
            }
            if not drift:
                print(f"[ok]   authenticationConfiguration already at {DESIRED}")  # noqa: T201
                return

            changed = ", ".join(
                f"{key}: {stored.get(key)!r} -> {value!r}"
                for key, value in sorted(drift.items())
            )
            cursor.execute(_UPDATE_SQL, (json.dumps(DESIRED),))
            print(f"[done] authenticationConfiguration updated: {changed}")  # noqa: T201
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[fail] {exc}", file=sys.stderr)  # noqa: T201
        sys.exit(1)
