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

Domains are declared in ``DOMAIN_CONFIG``.  Which assets belong to each is set
from ol-data-platform's dbt manifest and domain rules, not here: by the
om-ingest-dbt job for dbt's tables, and by the om-ingest-domains job
(domain_assignment.py) for the other lake tables and Superset.

Custom properties are declared in ``CUSTOM_PROPERTY_CONFIG``, per entity type.
A missing property is added and one whose description or displayName differs
is put back.  A property removed from the config is left on the type, because
deleting it drops its value from every entity that has one.

Access policies, roles, teams and bots are not managed here: the stack
declares them with the OpenMetadata providers
(ol_infrastructure.providers.openmetadata), which connect as an admin, from
substructure/open_metadata access_control.py.

The business glossary is not managed here either: ol-data-platform's
``glossary/`` declares it, and the ol-data-glossary-sync pipeline publishes it
with ``ol-dbt glossary sync`` as glossary-sync-bot.
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


# Desired state for each domain, keyed by domain name.  Domains split the
# warehouse by business area rather than source platform, because dim_user and
# the combined marts span every platform.  Assets are assigned to them by the
# rules in ol-data-platform src/ol_dbt/openmetadata_domains.yml, which refer to
# these names; the om-ingest-dbt and om-ingest-domains jobs skip a name that
# does not exist here.
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

# displayName is set when a domain is created and never patched afterwards:
# IngestionBotPolicy, which this job's token carries, denies EditDisplayName
# on every resource.
_DOMAIN_PATCHABLE_FIELDS: list[str] = ["description", "domainType"]

# Custom properties, keyed by entity type and then property name.  propertyType
# names one of OpenMetadata's property types (e.g. "string"), and cannot be
# changed once the property exists.
#
# ol-data-platform's `ol-dbt metrics sync` writes metricFileSha256 on every
# Metric it syncs, so the sync authors a version of the metric that it takes as
# its drift baseline.  The server rejects the sync's PATCH without it.
CUSTOM_PROPERTY_CONFIG: dict[str, dict[str, dict[str, str]]] = {
    "metric": {
        "metricFileSha256": {
            "propertyType": "string",
            "displayName": "Metric file SHA-256",
            "description": (
                "SHA-256 of the metrics/ file this metric was last synced from. "
                "Written by ol-dbt metrics sync; do not edit."
            ),
        },
    },
}

# A PUT of an existing property replaces it, and the server records a change to
# either field.  The PUT is authorized as Create on the type, so the bot's
# EditDisplayName deny does not apply.
_CUSTOM_PROPERTY_UPDATABLE_FIELDS: list[str] = ["description", "displayName"]


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


def _get_by_name(
    collection: str, name: str, fields: str | None = None
) -> dict[str, Any] | None:
    """Fetch an entity by name, or None if it does not exist.

    :param collection: The API collection, e.g. ``"domains"``.
    :param name: The entity's name.
    :param fields: Comma-separated fields to include, e.g. ``"customProperties"``.
    :raises requests.HTTPError: On any non-2xx response other than 404.
    """
    get_resp = requests.get(
        f"{OM_SERVER_URL}/v1/{collection}/name/{name}",
        headers=_AUTH_HEADER,
        params={"fields": fields} if fields else None,
        timeout=30,
    )
    if get_resp.status_code == requests.codes.not_found:
        return None
    get_resp.raise_for_status()
    return get_resp.json()


def _add_ops(
    current: dict[str, Any], wanted: dict[str, Any], fields: list[str]
) -> list[dict[str, Any]]:
    """Build JSON Patch ``add`` operations for the fields that differ.

    ``add`` rather than ``replace``, because the server leaves an empty field
    out of the entity and ``replace`` fails on a missing member.

    :param current: The entity as the server returned it.
    :param wanted: Mapping of field names to desired values.
    :param fields: The fields of *wanted* to compare.
    :returns: One operation per field whose value differs.
    """
    return [
        {"op": "add", "path": f"/{field}", "value": wanted[field]}
        for field in fields
        if current.get(field) != wanted[field]
    ]


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

    ops = _add_ops(domain, wanted, _DOMAIN_PATCHABLE_FIELDS)
    _apply_patch(f"domain {name}", f"{OM_SERVER_URL}/v1/domains/{domain['id']}", ops)


def _reconcile_custom_property(
    entity_type: str, name: str, desired: dict[str, str]
) -> None:
    """Add the custom property if it is missing, else put back a changed one.

    :param entity_type: The entity type the property extends, e.g. ``"metric"``.
    :param name: Property name, e.g. ``"metricFileSha256"``.
    :param desired: The property's propertyType name, displayName and description.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    :raises ValueError: If the entity type or property type does not exist, or
        the property exists with another property type.
    """
    label = f"{entity_type} custom property {name}"
    entity_def = _get_by_name("metadata/types", entity_type, fields="customProperties")
    if entity_def is None:
        msg = f"entity type {entity_type!r} does not exist"
        raise ValueError(msg)

    current = next(
        (prop for prop in entity_def["customProperties"] if prop["name"] == name),
        None,
    )
    changed: list[str] = []
    if current is not None:
        # The server's updater matches properties on name and type but stores
        # them by name, so a PUT with another type does not replace the
        # property cleanly.  Changing it needs a person.
        if current["propertyType"]["name"] != desired["propertyType"]:
            msg = (
                f"exists with propertyType {current['propertyType']['name']!r}, "
                f"expected {desired['propertyType']!r}"
            )
            raise ValueError(msg)
        changed = [
            field
            for field in _CUSTOM_PROPERTY_UPDATABLE_FIELDS
            if current.get(field) != desired[field]
        ]
        if not changed:
            print(f"[ok]   {label}: already at desired state — skipping")  # noqa: T201
            return

    property_type = _get_by_name("metadata/types", desired["propertyType"])
    if property_type is None:
        msg = f"property type {desired['propertyType']!r} does not exist"
        raise ValueError(msg)

    put_resp = requests.put(
        f"{OM_SERVER_URL}/v1/metadata/types/{entity_def['id']}",
        headers=_AUTH_HEADER,
        json={
            "name": name,
            "displayName": desired["displayName"],
            "description": desired["description"],
            "propertyType": {"id": property_type["id"], "type": "type"},
        },
        timeout=30,
    )
    put_resp.raise_for_status()
    if current is None:
        print(f"[done] {label}: created")  # noqa: T201
    else:
        print(f"[done] {label}: updated {', '.join(changed)}")  # noqa: T201


def main() -> None:
    """Reconcile every tag, domain and custom property.

    Exits non-zero if any step fails.
    """
    errors: list[str] = []

    steps: list[tuple[str, Callable[[], None]]] = [
        *((fqn, partial(_reconcile_tag, fqn, tag)) for fqn, tag in TAG_CONFIG.items()),
        *(
            (f"domain {name}", partial(_reconcile_domain, name, domain))
            for name, domain in DOMAIN_CONFIG.items()
        ),
        *(
            (
                f"{entity_type} custom property {name}",
                partial(_reconcile_custom_property, entity_type, name, prop),
            )
            for entity_type, props in CUSTOM_PROPERTY_CONFIG.items()
            for name, prop in props.items()
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
