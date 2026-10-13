"""OpenMetadata server-side configuration bootstrap.

Applies settings that live in the OM server's own database and cannot be
expressed through connector workflow configs.  Designed to be idempotent —
safe to re-run on every deploy; each step checks current state before writing.

Tag configuration is declared in ``TAG_CONFIG`` below.  Each entry maps a
fully-qualified tag name to the subset of tag fields we want to manage.
Fields not listed are left untouched.  To activate a tag for
AutoClassificationWorkflow scanning, set ``autoClassificationEnabled: True``.

Currently managed tags
──────────────────────
PII.Sensitive / PII.NonSensitive
  Both ship with ``autoClassificationEnabled=False``.  The
  AutoClassificationWorkflow (trino-classifier CronOMJob) queries the server
  for tags where this flag is True before it starts scanning; if none are
  found it processes ~2,700 tables but writes 0 tags.

PersonalData.Personal / PersonalData.SpecialCategory
  GDPR personal-data categories.  These ship with *zero recognizers*, so
  ``autoClassificationEnabled=False`` is intentional here: enabling the flag
  without recognizers is a no-op.  Once recognizers are configured in the OM
  UI (Settings → Classifications → PersonalData → <tag> → Edit → Recognizers),
  flip the flag to True and redeploy to start applying the tag automatically.

Teams are declared in ``TEAM_CONFIG``.  Each is created as a ``Group`` (the
only team type that can own assets and hold users) under ``Organization``.
Membership is not managed here: it is assigned in the OpenMetadata UI, so a
team that already exists is never sent a ``users`` list.
"""

import os
import sys
from collections.abc import Callable
from functools import partial
from typing import Any

import requests

OM_SERVER_URL = os.environ["OM_SERVER_URL"].rstrip("/")
_AUTH_HEADER = {"Authorization": f"Bearer {os.environ['OM_BOT_JWT_TOKEN']}"}
_PATCH_HEADER = {**_AUTH_HEADER, "Content-Type": "application/json-patch+json"}

# Desired state for each tag.
#
# Supported fields (all optional; omitted fields are not touched):
#   autoClassificationEnabled  bool   Whether the tag is a candidate for
#                                     AutoClassificationWorkflow scanning.
#                                     Has no effect if the tag has no
#                                     recognizers configured.
#   autoClassificationPriority int    Tie-breaking priority when multiple tags
#                                     could apply to the same column (higher
#                                     wins).  Default in OM is 50.
#   description                str    The tag's description, as Markdown.
TAG_CONFIG: dict[str, dict[str, Any]] = {
    # ── PII classification ────────────────────────────────────────────────
    # Ships with 45 content + column-name recognizers (email, SSN, credit
    # card, phone, person-name, spaCy NER, …).  Flag defaults False.
    "PII.Sensitive": {
        "autoClassificationEnabled": True,
        "autoClassificationPriority": 50,
    },
    # Ships with 8 recognizers (date, phone, URL, location, spaCy NRP, …).
    "PII.NonSensitive": {
        "autoClassificationEnabled": True,
        "autoClassificationPriority": 40,  # lower priority than Sensitive
    },
    # ── PersonalData classification (GDPR) ───────────────────────────────
    # Ships with 0 recognizers — autoClassification is intentionally False
    # until recognizers are defined.  To activate, add recognizers via the
    # OM UI then flip the flag to True here and redeploy.
    "PersonalData.Personal": {
        "autoClassificationEnabled": False,
        "autoClassificationPriority": 50,
    },
    "PersonalData.SpecialCategory": {
        "autoClassificationEnabled": False,
        "autoClassificationPriority": 60,  # higher than Personal when active
    },
    # ── Tier classification ──────────────────────────────────────────────
    # Tier is criticality only.  The tiers themselves are assigned from dbt
    # meta (ol-data-platform src/ol_dbt/dbt_project.yml) by the om-ingest-dbt
    # job; these descriptions replace the generic ones OpenMetadata ships so
    # the UI says what each tier means here.
    "Tier.Tier1": {
        "description": (
            "Assets with a data contract. Assigned per asset by the data platform team."
        ),
    },
    "Tier.Tier2": {
        "description": "Dimensional, mart and reporting models without a contract.",
    },
    "Tier.Tier3": {"description": "Intermediate models."},
    "Tier.Tier4": {"description": "Staging and external models."},
    "Tier.Tier5": {"description": "Raw and migration tables."},
}

# Fields from TAG_CONFIG that map directly to a JSON Patch /path.
_PATCHABLE_FIELDS: list[str] = [
    "autoClassificationEnabled",
    "autoClassificationPriority",
    "description",
]


# Desired state for each team, keyed by team name.  Asset ownership and alert
# routing refer to these names, so renaming one orphans what it owns.
TEAM_CONFIG: dict[str, dict[str, str]] = {
    "data-platform": {
        "displayName": "Data Platform",
        "description": (
            "Builds and operates the data platform. Default owner of warehouse assets."
        ),
    },
    "analytics": {
        "displayName": "Analytics",
        "description": "Analysts who work with the warehouse.",
    },
}

_TEAM_TYPE = "Group"

# displayName is set when a team is created and never patched afterwards:
# IngestionBotPolicy, which this job's token carries, denies EditDisplayName
# on every resource.
_TEAM_PATCHABLE_FIELDS: list[str] = ["description"]


def _apply_patch(label: str, url: str, ops: list[dict[str, Any]]) -> None:
    """Send a JSON Patch, or report that there is nothing to change.

    :param label: What is being reconciled, for the log line.
    :param url: The entity's ``/v1/<collection>/<id>`` URL.
    :param ops: JSON Patch operations; empty when the entity is already right.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    """
    if not ops:
        print(f"[ok]   {label}: already at desired state — skipping")  # noqa: T201
        return

    changed = ", ".join(f"{op['path'].lstrip('/')}={op['value']!r}" for op in ops)
    patch_resp = requests.patch(url, headers=_PATCH_HEADER, json=ops, timeout=30)
    patch_resp.raise_for_status()
    print(f"[done] {label}: patched {changed}")  # noqa: T201


def _reconcile_tag(fqn: str, desired: dict[str, Any]) -> None:
    """Read the current tag state and PATCH only fields that differ.

    Uses a read-before-write pattern to avoid unnecessary audit-log churn.
    Only fields present in *desired* are compared and potentially patched;
    all other tag fields are left untouched.

    :param fqn: Fully-qualified tag name, e.g. ``"PII.Sensitive"``.
    :param desired: Mapping of field names to desired values.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    """
    get_resp = requests.get(
        f"{OM_SERVER_URL}/v1/tags/name/{fqn}",
        headers=_AUTH_HEADER,
        timeout=30,
    )
    get_resp.raise_for_status()
    tag = get_resp.json()

    ops = [
        {"op": "replace", "path": f"/{field}", "value": value}
        for field in _PATCHABLE_FIELDS
        if (value := desired.get(field)) is not None and tag.get(field) != value
    ]

    _apply_patch(fqn, f"{OM_SERVER_URL}/v1/tags/{tag['id']}", ops)


def _reconcile_team(name: str, desired: dict[str, str]) -> None:
    """Create the team if it is missing, else PATCH the fields that differ.

    A missing team is created with POST and no ``parents``, which the server
    places under ``Organization``.  An existing team is only ever patched on
    ``_TEAM_PATCHABLE_FIELDS``, so the members assigned in the UI are kept.

    :param name: Team name, e.g. ``"data-platform"``.
    :param desired: Mapping of team field names to desired values.
    :raises requests.HTTPError: On any unexpected non-2xx response from the OM API.
    :raises ValueError: If the team exists with a type other than ``Group``.
    """
    get_resp = requests.get(
        f"{OM_SERVER_URL}/v1/teams/name/{name}",
        headers=_AUTH_HEADER,
        timeout=30,
    )
    if get_resp.status_code == requests.codes.not_found:
        post_resp = requests.post(
            f"{OM_SERVER_URL}/v1/teams",
            headers=_AUTH_HEADER,
            # isJoinable defaults to true, which would let any user add
            # themselves to a team that owns assets.
            json={
                "name": name,
                "teamType": _TEAM_TYPE,
                "isJoinable": False,
                **desired,
            },
            timeout=30,
        )
        post_resp.raise_for_status()
        print(f"[done] team {name}: created")  # noqa: T201
        return
    get_resp.raise_for_status()
    team = get_resp.json()

    # The server refuses to change a Group's type, and a non-Group team cannot
    # own assets, so this needs a person to sort out.
    if team["teamType"] != _TEAM_TYPE:
        msg = f"exists with teamType {team['teamType']!r}, expected {_TEAM_TYPE!r}"
        raise ValueError(msg)

    # "add" replaces a member that is present and sets one that is absent, which
    # "replace" rejects; a team made by hand may have no description.
    ops = [
        {"op": "add", "path": f"/{field}", "value": desired[field]}
        for field in _TEAM_PATCHABLE_FIELDS
        if team.get(field) != desired[field]
    ]

    _apply_patch(f"team {name}", f"{OM_SERVER_URL}/v1/teams/{team['id']}", ops)


def main() -> None:
    """Reconcile every tag and team; exit non-zero if any step fails."""
    errors: list[str] = []

    steps: list[tuple[str, Callable[[], None]]] = [
        *((fqn, partial(_reconcile_tag, fqn, tag)) for fqn, tag in TAG_CONFIG.items()),
        *(
            (f"team {name}", partial(_reconcile_team, name, team))
            for name, team in TEAM_CONFIG.items()
        ),
    ]
    for label, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            msg = f"[fail] {label}: {exc}"
            print(msg, file=sys.stderr)  # noqa: T201
            errors.append(msg)

    if errors:
        print(f"\n{len(errors)} error(s) — bootstrap incomplete.", file=sys.stderr)  # noqa: T201
        sys.exit(1)

    print("\nBootstrap complete.")  # noqa: T201


if __name__ == "__main__":
    main()
