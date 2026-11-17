# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "httpx==0.28.1",
#     "ol-llm[memory]==0.0.14",
#     "psycopg[binary]==3.3.8",
# ]
#
# [tool.uv]
# exclude-newer = "7d"
# exclude-newer-package = { ol-llm = "0d" }
# # ol-llm's README overrides for opik 2.2.143 in a slimmed runtime install, as in
# # ol-llm's python/smoke/pyproject.toml: the cap entries for the packages the
# # slimming entries keep, then the slimming entries. The constraints keep
# # advisory scanners, which read the lock, off litellm and filelock floors that
# # the overrides make unreachable anyway.
# constraint-dependencies = ["filelock>=4.2.1", "litellm>=1.95.0"]
# override-dependencies = [
#     "httpx[http2]>=0.28.1,<1",
#     "jinja2>=3.1.6,<4",
#     "orjson>=3.9.14; platform_python_implementation == 'CPython' and platform_machine in 'x86_64 AMD64 aarch64 arm64 ARM64 i686 x86' and (sys_platform != 'win32' or platform_machine != 'ARM64' or python_version >= '3.11')",  # noqa: E501
#     "pydantic>=2.14,<3",
#     "pydantic-settings>=2.14.1,<3",
#     "rich",
#     "sentry-sdk>=2.0.0",
#     "tenacity>=8.2.3,!=8.4.0,<9.2.0",
#     "tqdm>=4.42.1",
#     "uuid6",
#     "boto3-stubs; sys_platform == 'never'",
#     "click; sys_platform == 'never'",
#     "litellm; sys_platform == 'never'",
#     "openai; sys_platform == 'never'",
#     "pytest; sys_platform == 'never'",
#     "rapidfuzz; sys_platform == 'never'",
#     "tree-sitter; sys_platform == 'never'",
#     "tree-sitter-javascript; sys_platform == 'never'",
#     "tree-sitter-typescript; sys_platform == 'never'",
#     "watchfiles; sys_platform == 'never'",
# ]
# ///
"""Forget the shared memory of learners whose Keycloak account no longer exists.

Runs on a schedule in the Hindsight namespace (../__main__.py). A learner's bank is
``u-<keycloak sub>``. Each run lists the learner banks from Hindsight's database,
asks Keycloak whether each user still exists, and forgets the banks of the users
Keycloak reports missing, through ol-llm's forget path as the platform admin
client.

Only Keycloak's own answer for a missing user (a 404 whose JSON body is
``{"error": "User not found"}``) means "deleted". Any other answer, including a
404 from something in front of Keycloak, skips that learner for this run, so an
outage never reads as a deletion. The run checks that the realm answers and has
users before the scan and again after it, which catches a wrong realm or URL and
a realm that went away mid-scan. If more learners would be forgotten than
``FORGET_CAP``, the run forgets none of them and exits ``CAP_EXCEEDED_STATUS``,
so a misread cannot wipe every bank at once; an operator who confirms a real
backlog raises the cap in the stack config.

Logs carry counts, never learner ids. httpx and httpx2 log every request URL at
INFO, and those URLs name the learner, so only this script's logger logs at
INFO.
"""

import logging
import os
import re
import sys

import httpx
import psycopg
from ol_llm.memory import MemoryStoreError, SyncMemory, build_memory
from ol_llm.tracing import HttpxBearerAuth, KeycloakClient, KeycloakTokens

log = logging.getLogger("hindsight-forget")

BANK_PREFIX = "u-"
LEARNER_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
USE_CASE = "account-deletion"
KEYCLOAK_TIMEOUT_SECONDS = 10
USER_NOT_FOUND = {"error": "User not found"}
# The CronJob fails the Job on this status without a retry, which would only
# find the same learners again.
CAP_EXCEEDED_STATUS = 3


def learner_banks(conninfo: str) -> list[str]:
    """Return the learner ids that have a bank.

    :param conninfo: libpq connection string for Hindsight's database.
    :returns: the learner ids (Keycloak user ids).
    :rtype: list[str]
    """
    with psycopg.connect(conninfo) as conn:
        rows = conn.execute(
            "SELECT bank_id FROM public.banks WHERE bank_id LIKE %s",
            (f"{BANK_PREFIX}%",),
        ).fetchall()
    ids = [row[0].removeprefix(BANK_PREFIX) for row in rows]
    return [learner for learner in ids if LEARNER_ID_RE.match(learner)]


def deleted_learners(
    keycloak: httpx.Client, realm: str, learners: list[str]
) -> tuple[list[str], int]:
    """Split learners into those Keycloak reports deleted and those it could not answer.

    :param keycloak: client for the Keycloak admin API, authenticated.
    :param realm: the realm the learners belong to.
    :param learners: the learner ids to check.
    :returns: the deleted learners, and how many lookups failed.
    :rtype: tuple[list[str], int]
    :raises RuntimeError: if the realm does not answer with a user count above
        zero, before or after the scan.
    """
    check_realm(keycloak, realm)
    deleted: list[str] = []
    failed = 0
    for learner in learners:
        try:
            response = keycloak.get(f"/admin/realms/{realm}/users/{learner}")
        except httpx.HTTPError as exc:
            log.warning("user lookup failed: %s", type(exc).__name__)
            failed += 1
            continue
        if response.status_code == httpx.codes.OK:
            continue
        if response.status_code == httpx.codes.NOT_FOUND and _json(response) == (
            USER_NOT_FOUND
        ):
            deleted.append(learner)
        else:
            log.warning("user lookup returned %s", response.status_code)
            failed += 1
    check_realm(keycloak, realm)
    return deleted, failed


def check_realm(keycloak: httpx.Client, realm: str) -> None:
    """Make sure the realm answers and has users.

    :param keycloak: client for the Keycloak admin API, authenticated.
    :param realm: the realm.
    :raises RuntimeError: if it does not answer with a user count above zero.
    """
    count = keycloak.get(f"/admin/realms/{realm}/users/count")
    if count.status_code != httpx.codes.OK or int(count.text) <= 0:
        msg = (
            f"realm {realm!r} user count check returned {count.status_code}; "
            "refusing to treat any learner as deleted"
        )
        raise RuntimeError(msg)


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def main() -> int:
    """Run one pass and return the process exit code.

    :returns: 0 when every check and forget succeeded, ``CAP_EXCEEDED_STATUS``
        when the cap stopped the run, 1 otherwise.
    :rtype: int
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.setLevel(logging.INFO)
    cap = int(os.environ["FORGET_CAP"])
    realm = os.environ["KEYCLOAK_REALM"]
    keycloak_url = os.environ["KEYCLOAK_URL"]
    conninfo = psycopg.conninfo.make_conninfo(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        sslmode="require",
    )

    learners = learner_banks(conninfo)
    tokens = KeycloakTokens(
        KeycloakClient(
            token_url=f"{keycloak_url}/realms/{realm}/protocol/openid-connect/token",
            client_id=os.environ["KEYCLOAK_LOOKUP_CLIENT_ID"],
            client_secret=os.environ["KEYCLOAK_LOOKUP_CLIENT_SECRET"],
        )
    )
    with httpx.Client(
        base_url=keycloak_url,
        auth=HttpxBearerAuth(tokens, host=httpx.URL(keycloak_url).host),
        headers={"Accept": "application/json"},
        timeout=KEYCLOAK_TIMEOUT_SECONDS,
    ) as keycloak:
        deleted, lookup_failed = deleted_learners(keycloak, realm, learners)
    tokens.close()

    summary = {
        "banks_checked": len(learners),
        "deleted_in_keycloak": len(deleted),
        "lookups_failed": lookup_failed,
        "cap": cap,
    }
    if len(deleted) > cap:
        log.error("forget cap exceeded, forgetting none: %s", summary)
        return CAP_EXCEEDED_STATUS

    admin = SyncMemory(build_memory()).admin(use_case=USE_CASE)
    forgotten = 0
    forget_failed = 0
    for learner in deleted:
        try:
            admin.forget(learner)
        except MemoryStoreError as exc:
            log.warning("forget failed: %s", type(exc).__name__)
            forget_failed += 1
        else:
            forgotten += 1
    summary |= {"forgotten": forgotten, "forgets_failed": forget_failed}
    log.info("run complete: %s", summary)
    return 1 if lookup_failed or forget_failed else 0


if __name__ == "__main__":
    sys.exit(main())
