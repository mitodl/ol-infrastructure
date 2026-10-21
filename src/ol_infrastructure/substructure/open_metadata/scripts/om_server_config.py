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

Domains are declared in ``DOMAIN_CONFIG``.  Which assets belong to each is set
from ol-data-platform's dbt manifest, not here.
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

# Desired state for each domain, keyed by domain name.  Domains split the
# warehouse by business area rather than source platform, because dim_user and
# the combined marts span every platform.  Assets are assigned to them by the
# rules in ol-data-platform src/ol_dbt/openmetadata_domains.yml, which refer to
# these names; the om-ingest-dbt job skips a name that does not exist here.
# Every domain is "Aggregate": each holds raw tables from the source apps as
# well as the models built for consumers.
DOMAIN_CONFIG: dict[str, dict[str, str]] = {
    "learners-identity": {
        "displayName": "Learners & Identity",
        "description": (
            "Who our learners are: user accounts on each platform, profiles, "
            "and the cross-platform identity in dim_user."
        ),
    },
    "courses-content": {
        "displayName": "Courses & Content",
        "description": (
            "Courses, runs, programs and their content: catalog pages, course "
            "structure, videos, problems, instructors, topics, and the "
            "resources published to MIT Learn."
        ),
    },
    "enrollments-progress": {
        "displayName": "Enrollments & Progress",
        "description": (
            "Enrollments in course runs and programs, grades, certificates and "
            "learner progress."
        ),
    },
    "commerce": {
        "displayName": "Commerce",
        "description": (
            "Products, orders, payments, discounts, coupons and financial assistance."
        ),
    },
    "b2b": {
        "displayName": "B2B",
        "description": (
            "Organizations, contracts and their learners, and corporate sales."
        ),
    },
    "feedback": {
        "displayName": "Feedback",
        "description": (
            "Learner feedback from support tickets, forums, chatbot ratings and "
            "content feedback, with its categories and sentiment."
        ),
    },
    "engagement": {
        "displayName": "Engagement",
        "description": (
            "What learners do in courses and on MIT Learn: tracking events, "
            "video, problem, discussion and navigation activity, and chatbot use."
        ),
    },
}

_DOMAIN_TYPE = "Aggregate"

# displayName is create-only for the same reason as teams.
_DOMAIN_PATCHABLE_FIELDS: list[str] = ["description", "domainType"]

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


def _get_by_name(collection: str, name: str) -> dict[str, Any] | None:
    """Fetch an entity by name, or None if it does not exist.

    :param collection: The API collection, e.g. ``"teams"``.
    :param name: The entity's name.
    :raises requests.HTTPError: On any non-2xx response other than 404.
    """
    get_resp = requests.get(
        f"{OM_SERVER_URL}/v1/{collection}/name/{name}",
        headers=_AUTH_HEADER,
        timeout=30,
    )
    if get_resp.status_code == requests.codes.not_found:
        return None
    get_resp.raise_for_status()
    return get_resp.json()


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
    team = _get_by_name("teams", name)
    if team is None:
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


def _reconcile_domain(name: str, desired: dict[str, str]) -> None:
    """Create the domain if it is missing, else PATCH the fields that differ.

    :param name: Domain name, e.g. ``"commerce"``.
    :param desired: Mapping of domain field names to desired values.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    """
    wanted = {**desired, "domainType": _DOMAIN_TYPE}
    domain = _get_by_name("domains", name)
    if domain is None:
        post_resp = requests.post(
            f"{OM_SERVER_URL}/v1/domains",
            headers=_AUTH_HEADER,
            json={"name": name, **wanted},
            timeout=30,
        )
        post_resp.raise_for_status()
        print(f"[done] domain {name}: created")  # noqa: T201
        return

    ops = [
        {"op": "add", "path": f"/{field}", "value": wanted[field]}
        for field in _DOMAIN_PATCHABLE_FIELDS
        if domain.get(field) != wanted[field]
    ]

    _apply_patch(f"domain {name}", f"{OM_SERVER_URL}/v1/domains/{domain['id']}", ops)


# ── Access control ────────────────────────────────────────────────────────
# A user's own roles come from the `roles` claim that Keycloak sends at login
# (authorizer useRolesFromProvider in applications/open_metadata, mapping in
# substructure/keycloak ol_data_platform.py OPEN_METADATA_ROLE_BY_REALM_ROLE):
# ol-platform-admin and ol-data-engineer -> DataEngineer, ol-researcher ->
# DataContributor, the two analyst roles and every other user -> DataConsumer.
# No realm role maps to Admin; admins come from initialAdmins.  The claim
# replaces the roles a user holds, so roles assigned to a person in the UI do
# not survive their next login.  On top of their own roles every user inherits
# DataConsumer and OrganizationPolicy from the Organization team, and a team's
# members inherit its defaultRoles.
#
# DataEngineer and DataContributor are the custom roles the claim names.  They
# grant only what DataConsumer does not: DataConsumer already lets everyone
# view everything and edit descriptions, tags, glossary terms, tier and
# certification.

# Entity types that hold warehouse and BI metadata, as opposed to governance
# entities (glossaries, teams, policies).
_DATA_ASSET_RESOURCES: list[str] = [
    "apiEndpoint",
    "chart",
    "container",
    "dashboard",
    "dashboardDataModel",
    "database",
    "databaseSchema",
    "metric",
    "mlmodel",
    "pipeline",
    "searchIndex",
    "storedProcedure",
    "table",
    "topic",
]

# Desired state for each custom policy, keyed by policy name.  The server
# stores rules, resources and operations sorted, so they are compared as sets.
POLICY_CONFIG: dict[str, dict[str, Any]] = {
    "DataEngineerPolicy": {
        "displayName": "Data Engineer Policy",
        "description": (
            "Technical metadata, lineage and data quality tests on data assets."
        ),
        "rules": [
            {
                "name": "DataEngineerPolicy-TechnicalMetadata",
                "description": "Edit technical metadata and lineage of data assets.",
                "effect": "allow",
                "resources": _DATA_ASSET_RESOURCES,
                "operations": [
                    "EditCustomFields",
                    "EditDescription",
                    "EditDisplayName",
                    "EditLineage",
                    "EditTags",
                ],
            },
            {
                "name": "DataEngineerPolicy-DataQuality",
                "description": (
                    "Add and edit the tests on a table, and triage their failures."
                ),
                "effect": "allow",
                "resources": ["table", "testCase", "testSuite"],
                "operations": ["CreateTests", "EditTests"],
            },
            {
                "name": "DataEngineerPolicy-TestCases",
                "description": "Create and edit test cases and test suites.",
                "effect": "allow",
                "resources": ["testCase", "testSuite"],
                "operations": ["Create", "EditAll"],
            },
        ],
    },
    "DataContributorPolicy": {
        "displayName": "Data Contributor Policy",
        "description": "Save queries against data assets.",
        "rules": [
            {
                # A saved query is owned by its author, so OrganizationPolicy's
                # owner rule covers editing and deleting it afterwards.
                "name": "DataContributorPolicy-Queries",
                "description": "Save SQL queries to data assets.",
                "effect": "allow",
                "resources": ["query"],
                "operations": ["Create"],
            },
        ],
    },
}

# Desired state for each custom role, keyed by role name.  These names are the
# values Keycloak sends in the `roles` claim, so renaming one here without
# renaming its Keycloak client role leaves its holders with no role.
ROLE_CONFIG: dict[str, dict[str, Any]] = {
    "DataEngineer": {
        "displayName": "Data Engineer",
        "description": (
            "Maintains technical metadata, lineage and data quality tests. "
            "Granted by the ol-data-engineer and ol-platform-admin Keycloak "
            "roles."
        ),
        "policies": ["DataEngineerPolicy"],
    },
    "DataContributor": {
        "displayName": "Data Contributor",
        "description": (
            "Documents data assets and saves queries against them. Granted by "
            "the ol-researcher Keycloak role."
        ),
        "policies": ["DataContributorPolicy"],
    },
}

# The exact defaultRoles of each listed team.  data-platform members are the
# stewards: DataSteward adds owners, display names, lineage and running
# ingestion to what DataConsumer allows.  The team is also who reviews data
# contracts, but in ol-data-platform pull requests: contracts are synced to
# OpenMetadata already Approved, so OpenMetadata's own approval step (an
# In Review contract that only its `reviewers` can approve) is not used.
TEAM_DEFAULT_ROLES: dict[str, list[str]] = {
    "data-platform": ["DataSteward"],
}


def _get_by_name_with_fields(
    collection: str, name: str, fields: str
) -> dict[str, Any] | None:
    """Fetch an entity by name with extra fields, or None if it does not exist.

    :param collection: The API collection, e.g. ``"roles"``.
    :param name: The entity's name.
    :param fields: Comma-separated fields to include, e.g. ``"policies"``.
    :raises requests.HTTPError: On any non-2xx response other than 404.
    """
    get_resp = requests.get(
        f"{OM_SERVER_URL}/v1/{collection}/name/{name}",
        headers=_AUTH_HEADER,
        params={"fields": fields},
        timeout=30,
    )
    if get_resp.status_code == requests.codes.not_found:
        return None
    get_resp.raise_for_status()
    return get_resp.json()


def _entity_refs(
    collection: str, entity_type: str, names: list[str]
) -> list[dict[str, str]]:
    """Resolve entity names to the references a PATCH expects.

    :param collection: The API collection, e.g. ``"roles"``.
    :param entity_type: The reference type, e.g. ``"role"``.
    :param names: Entity names, every one of which must exist.
    :raises ValueError: If a named entity does not exist.
    """
    refs = []
    for name in names:
        entity = _get_by_name(collection, name)
        if entity is None:
            msg = f"{entity_type} {name!r} does not exist"
            raise ValueError(msg)
        refs.append({"id": entity["id"], "type": entity_type})
    return refs


def _normalized_rules(rules: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Reduce policy rules to a form that ignores the server's reordering.

    :param rules: Rules as sent to or returned by the policies API.
    """
    return sorted(
        (
            rule["name"],
            rule["effect"],
            frozenset(resource.lower() for resource in rule["resources"]),
            frozenset(rule["operations"]),
            rule.get("condition"),
            rule.get("description"),
        )
        for rule in rules
    )


def _reconcile_policy(name: str, desired: dict[str, Any]) -> None:
    """Create the policy if it is missing, else PATCH its description and rules.

    displayName is create-only, as for teams.

    :param name: Policy name, e.g. ``"DataEngineerPolicy"``.
    :param desired: The policy's displayName, description and rules.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    """
    policy = _get_by_name("policies", name)
    if policy is None:
        post_resp = requests.post(
            f"{OM_SERVER_URL}/v1/policies",
            headers=_AUTH_HEADER,
            json={"name": name, **desired},
            timeout=30,
        )
        post_resp.raise_for_status()
        print(f"[done] policy {name}: created")  # noqa: T201
        return

    ops = []
    if policy.get("description") != desired["description"]:
        ops.append(
            {"op": "add", "path": "/description", "value": desired["description"]}
        )
    if _normalized_rules(policy["rules"]) != _normalized_rules(desired["rules"]):
        ops.append({"op": "add", "path": "/rules", "value": desired["rules"]})

    _apply_patch(f"policy {name}", f"{OM_SERVER_URL}/v1/policies/{policy['id']}", ops)


def _reconcile_role(name: str, desired: dict[str, Any]) -> None:
    """Create the role if it is missing, else PATCH its description and policies.

    :param name: Role name, e.g. ``"DataEngineer"``.
    :param desired: The role's displayName, description and policy names.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    :raises ValueError: If a policy the role names does not exist.
    """
    role = _get_by_name_with_fields("roles", name, "policies")
    if role is None:
        post_resp = requests.post(
            f"{OM_SERVER_URL}/v1/roles",
            headers=_AUTH_HEADER,
            json={"name": name, **desired},
            timeout=30,
        )
        post_resp.raise_for_status()
        print(f"[done] role {name}: created")  # noqa: T201
        return

    ops = []
    if role.get("description") != desired["description"]:
        ops.append(
            {"op": "add", "path": "/description", "value": desired["description"]}
        )
    if {policy["name"] for policy in role["policies"]} != set(desired["policies"]):
        ops.append(
            {
                "op": "add",
                "path": "/policies",
                "value": _entity_refs("policies", "policy", desired["policies"]),
            }
        )

    _apply_patch(f"role {name}", f"{OM_SERVER_URL}/v1/roles/{role['id']}", ops)


def _reconcile_team_default_roles(team_name: str, role_names: list[str]) -> None:
    """Set a team's defaultRoles to exactly ``role_names``.

    :param team_name: Team name, e.g. ``"data-platform"``.
    :param role_names: Names of the roles every member of the team inherits.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    :raises ValueError: If the team or a named role does not exist.
    """
    team = _get_by_name_with_fields("teams", team_name, "defaultRoles")
    if team is None:
        msg = f"team {team_name!r} does not exist"
        raise ValueError(msg)

    ops = []
    if {role["name"] for role in team["defaultRoles"]} != set(role_names):
        ops.append(
            {
                "op": "add",
                "path": "/defaultRoles",
                "value": _entity_refs("roles", "role", role_names),
            }
        )

    _apply_patch(
        f"team {team_name} defaultRoles",
        f"{OM_SERVER_URL}/v1/teams/{team['id']}",
        ops,
    )


def _access_control_steps() -> list[tuple[str, Callable[[], None]]]:
    """Return the access-control steps, policies before the roles that use them.

    Runs after the team steps, because team defaultRoles need the team.
    """
    return [
        *(
            (f"policy {name}", partial(_reconcile_policy, name, policy))
            for name, policy in POLICY_CONFIG.items()
        ),
        *(
            (f"role {name}", partial(_reconcile_role, name, role))
            for name, role in ROLE_CONFIG.items()
        ),
        *(
            (
                f"team {name} defaultRoles",
                partial(_reconcile_team_default_roles, name, roles),
            )
            for name, roles in TEAM_DEFAULT_ROLES.items()
        ),
    ]


def main() -> None:
    """Reconcile every tag, team and domain; exit non-zero if any step fails."""
    errors: list[str] = []

    steps: list[tuple[str, Callable[[], None]]] = [
        *((fqn, partial(_reconcile_tag, fqn, tag)) for fqn, tag in TAG_CONFIG.items()),
        *(
            (f"team {name}", partial(_reconcile_team, name, team))
            for name, team in TEAM_CONFIG.items()
        ),
        *_access_control_steps(),
        *(
            (f"domain {name}", partial(_reconcile_domain, name, domain))
            for name, domain in DOMAIN_CONFIG.items()
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
