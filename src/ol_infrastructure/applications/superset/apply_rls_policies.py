#!/usr/bin/env python3
"""Apply OL governance RLS policies to Superset via the REST API.

This script is run as a post-deployment Kubernetes Job. It reads
ol_rls_policies.json (co-located in the same directory), resolves role and
table names to their Superset database IDs, then creates or updates each RLS
filter idempotently.

Environment variables (sourced from Vault-synced K8s secrets):
    SUPERSET_ADMIN_USER     - admin username for API authentication
    SUPERSET_ADMIN_PASSWORD - admin password for API authentication
    SUPERSET_URL            - internal Superset base URL (default: http://superset:8088)
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

SUPERSET_URL = os.environ.get("SUPERSET_URL", "http://superset:8088")
ADMIN_USER = os.environ.get("SUPERSET_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("SUPERSET_ADMIN_PASSWORD", "")
POLICY_FILE = Path(__file__).parent / "ol_rls_policies.json"
MAX_RETRIES = 10
RETRY_DELAY = 15  # seconds

if not ADMIN_PASSWORD:
    log.error(
        "Environment variable SUPERSET_ADMIN_PASSWORD is not set or is empty. "
        "A non-empty admin password is required for Superset API authentication."
    )
    sys.exit(1)


def wait_for_superset(session: requests.Session) -> None:
    """Poll until Superset's health endpoint responds, with linear backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(f"{SUPERSET_URL}/health", timeout=5)
            if resp.status_code == requests.codes.ok:
                log.info("Superset is healthy.")
                return
        except requests.RequestException as exc:
            log.warning("Attempt %d: Superset not ready (%s)", attempt, exc)
        delay = RETRY_DELAY * attempt
        log.info("Waiting %d seconds before retry...", delay)
        time.sleep(delay)
    log.error("Superset did not become healthy after %d attempts.", MAX_RETRIES)
    sys.exit(1)


def get_access_token(session: requests.Session) -> str:
    """Authenticate as admin and return a JWT access token."""
    resp = session.post(
        f"{SUPERSET_URL}/api/v1/security/login",
        json={
            "username": ADMIN_USER,
            "password": ADMIN_PASSWORD,
            "provider": "db",
            "refresh": False,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token: str = resp.json()["access_token"]
    log.info("Authenticated as %s.", ADMIN_USER)
    return token


def get_csrf_token(session: requests.Session) -> str:
    """Fetch a CSRF token required for state-mutating API calls."""
    resp = session.get(f"{SUPERSET_URL}/api/v1/security/csrf_token/", timeout=10)
    resp.raise_for_status()
    token: str = resp.json()["result"]
    return token


def build_name_to_id_map(session: requests.Session, endpoint: str) -> dict[str, int]:
    """Page through a Superset list endpoint and return {name: id} mapping."""
    result: dict[str, int] = {}
    page = 0
    page_size = 100
    while True:
        resp = session.get(
            f"{SUPERSET_URL}/api/v1/{endpoint}/",
            params={"q": json.dumps({"page": page, "page_size": page_size})},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("result", []):
            result[item["name"]] = item["id"]
        if len(data.get("result", [])) < page_size:
            break
        page += 1
    return result


def get_dataset_name_to_id(session: requests.Session) -> dict[str, int]:
    """Return mapping of dataset names to dataset IDs for all datasets.

    Datasets are indexed under two keys so that policies can use either format:
    - bare ``table_name`` (e.g. ``"instructor_module_report"``)
    - qualified ``schema.table_name`` (e.g.
      ``"ol_warehouse_production_reporting.instructor_module_report"``)

    When both a bare and a qualified key would collide (two datasets sharing the
    same table_name in different schemas) the qualified form is authoritative.
    """
    result: dict[str, int] = {}
    page = 0
    page_size = 100
    while True:
        resp = session.get(
            f"{SUPERSET_URL}/api/v1/dataset/",
            params={
                "q": json.dumps(
                    {
                        "page": page,
                        "page_size": page_size,
                        "columns": ["id", "table_name", "schema"],
                    }
                )
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("result", []):
            schema = item.get("schema") or ""
            table = item.get("table_name", "")
            dataset_id = item["id"]
            # Always index by bare table name so policies can use short names.
            result[table] = dataset_id
            # Also index by qualified name so policies can be explicit.
            if schema:
                result[f"{schema}.{table}"] = dataset_id
        if len(data.get("result", [])) < page_size:
            break
        page += 1
    return result


def get_existing_rls_filters(session: requests.Session) -> dict[str, int]:
    """Return {filter_name: filter_id} for all existing RLS filters."""
    result: dict[str, int] = {}
    page = 0
    page_size = 100
    while True:
        resp = session.get(
            f"{SUPERSET_URL}/api/v1/rowlevelsecurity/",
            params={"q": json.dumps({"page": page, "page_size": page_size})},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("result", []):
            result[item["name"]] = item["id"]
        if len(data.get("result", [])) < page_size:
            break
        page += 1
    return result


def apply_rls_filter(
    session: requests.Session,
    policy: dict[str, Any],
    role_map: dict[str, int],
    dataset_map: dict[str, int],
    existing: dict[str, int],
) -> None:
    """Create or update a single RLS filter from the policy template.

    Raises ``RuntimeError`` if any required role or table is missing so that
    partial or misconfigured security policies are never silently accepted.
    """
    name = policy["name"]

    missing_roles = [r for r in policy["roles"] if r not in role_map]
    if missing_roles:
        msg = (
            f"Required roles not found in Superset for filter '{name}': "
            f"{', '.join(missing_roles)}. "
            "Custom roles may not have been imported yet; the Job will retry."
        )
        raise RuntimeError(msg)
    role_ids = [role_map[r] for r in policy["roles"]]

    missing_tables = [t for t in policy["tables"] if t not in dataset_map]
    if missing_tables:
        msg = (
            f"Required datasets not found in Superset for filter '{name}': "
            f"{', '.join(missing_tables)}. "
            "Ensure dataset names in ol_rls_policies.json match registered datasets."
        )
        raise RuntimeError(msg)
    table_ids = [dataset_map[t] for t in policy["tables"]]

    payload = {
        "name": name,
        "description": policy.get("description", ""),
        "filter_type": policy["filter_type"],
        "clause": policy["clause"],
        "group_key": policy.get("group_key", ""),
        "roles": role_ids,
        "tables": table_ids,
    }

    if name in existing:
        filter_id = existing[name]
        resp = session.put(
            f"{SUPERSET_URL}/api/v1/rowlevelsecurity/{filter_id}",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Updated RLS filter '%s' (id=%d).", name, filter_id)
    else:
        resp = session.post(
            f"{SUPERSET_URL}/api/v1/rowlevelsecurity/",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        filter_id = resp.json()["id"]
        log.info("Created RLS filter '%s' (id=%d).", name, filter_id)


def main() -> None:
    policy_data = json.loads(POLICY_FILE.read_text())
    policies = policy_data["rls_filters"]

    session = requests.Session()
    wait_for_superset(session)

    token = get_access_token(session)
    session.headers.update({"Authorization": f"Bearer {token}"})

    csrf = get_csrf_token(session)
    session.headers.update({"X-CSRFToken": csrf, "Referer": SUPERSET_URL})

    log.info("Building role and dataset name->id maps...")
    role_map = build_name_to_id_map(session, "security/roles/search")
    dataset_map = get_dataset_name_to_id(session)
    existing = get_existing_rls_filters(session)

    log.info(
        "Found %d roles, %d datasets, %d existing RLS filters.",
        len(role_map),
        len(dataset_map),
        len(existing),
    )

    success_count = 0
    failure_count = 0
    failed_policies: list[str] = []

    for policy in policies:
        try:
            apply_rls_filter(session, policy, role_map, dataset_map, existing)
            success_count += 1
        except requests.HTTPError as exc:
            failure_count += 1
            failed_policies.append(policy["name"])
            log.exception(
                "HTTP error applying filter '%s' (response: %s).",
                policy["name"],
                exc.response.text if exc.response is not None else "no response",
            )
        except Exception:
            failure_count += 1
            failed_policies.append(policy["name"])
            log.exception("Error applying filter '%s'.", policy["name"])

    log.info(
        "RLS policy application complete. %d succeeded, %d failed.",
        success_count,
        failure_count,
    )
    if failure_count > 0:
        log.error(
            "One or more RLS policies failed to apply: %s",
            ", ".join(failed_policies),
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
