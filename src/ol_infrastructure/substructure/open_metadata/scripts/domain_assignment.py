"""Assign OpenMetadata domains to the assets the dbt ingestion does not describe.

The om-ingest-dbt job sets the domain of each table dbt builds or declares as a
source, from ``meta.openmetadata.domain`` in the uploaded manifest.  The
lakehouse code fills that key from the rules in ol-data-platform
src/ol_dbt/openmetadata_domains.yml and uploads the rules next to the manifest
as ``openmetadata_domains.json``.  This job applies the same rules, and the
domains they produce, to what dbt leaves out:

Lake tables dbt does not know
    Tables of the lake service (Trino) in the dbt layer schemas that are neither
    a dbt node nor a declared source, mostly raw tables Airbyte loads that no
    model reads.  Each gets the domain of the first rule its name matches, the
    same lookup the lakehouse code does for dbt nodes.  Tables dbt knows are
    left to the dbt job, including the ones it leaves without a domain.

Superset datasets (dashboardDataModel)
    The domain of the lake tables the dataset reads, found through the lineage
    the Superset ingestion records.  A Glue table counts as its Trino twin of
    the same schema and name.

Superset dashboards
    The domain of the datasets the dashboard reads, again from lineage.  The
    Superset ingestion leaves a dashboard's ``dataModels`` empty, so lineage is
    the only link between them.

OpenMetadata allows one domain per asset ("Multiple Domains are not allowed",
a default entity rule).  A dataset or dashboard whose tables span several
domains gets none, and is reported.  So is any asset no rule or lineage
resolves.  Neither fails the run.

Like the dbt job, which replaces a table's domain whenever its node names
another, a derived domain replaces whatever domain the asset carries, one set
in the UI included.  An asset with no derived domain keeps the one it has.

Left without domains on purpose:

Glue tables
    The Glue service holds a second copy of the lake tables Trino catalogs.
    Giving them domains would list every table twice under each domain.  The
    domain lookups here treat a Glue table as its Trino twin, so nothing needs
    them to carry one.

Pipelines (Dagster, Airbyte)
    A Dagster pipeline is one asset group of one code location, and an Airbyte
    pipeline one connection.  Each moves tables of several domains, and both
    are the data platform team's machinery rather than a business area's data.

Charts
    Not assigned yet.  A chart's domain would be that of its dashboard.

Environment variables
---------------------
OM_SERVER_URL              OpenMetadata API base URL.
OM_BOT_JWT_TOKEN           Ingestion-bot JWT (om-ingestion-bot secret).
OM_LAKE_SERVICE_NAME       OM database service holding the lake tables (Trino).
OM_SUPERSET_SERVICE_NAME   OM dashboard service holding Superset.
OM_AWS_REGION              AWS region for the S3 client.
OM_DBT_BUCKET              S3 bucket written by DbtS3ArtifactsResource.
OM_DBT_PREFIX              Key prefix of the lake's dbt artifacts.
OM_DBT_SCHEMA_INCLUDES     JSON list of schema regexes, the same ones the dbt
                           job ingests.  Only tables in these schemas are
                           assigned by rule.
OM_DOMAINS_DRY_RUN         JSON boolean.  ``true`` logs each change without
                           making it.
"""

import fnmatch
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
import requests

log = logging.getLogger("om-domains")

DOMAIN_RULES_ARTIFACT = "openmetadata_domains.json"
# The node types OpenMetadata turns into tables, as in the lakehouse code.
_TABLE_RESOURCE_TYPES = frozenset({"model", "snapshot", "seed"})
_PAGE_SIZE = 1000
_TIMEOUT_SECONDS = 60

RelationKey = tuple[str, str]


def relation_key(schema: str, name: str) -> RelationKey:
    """Key a lake table by schema and name, ignoring case."""
    return schema.casefold(), name.casefold()


def split_fqn(fqn: str) -> list[str]:
    """Split an OpenMetadata FQN, keeping a quoted part that holds a dot whole."""
    return [part.strip('"') for part in re.findall(r'"[^"]*"|[^.]+', fqn)]


def fqn_relation_key(fqn: str) -> RelationKey:
    """Key the table an FQN names, whichever service catalogs it."""
    *_, schema, name = split_fqn(fqn)
    return relation_key(schema, name)


@dataclass(frozen=True)
class DomainRules:
    """The ordered rules from openmetadata_domains.yml.

    Mirrors ``DomainRules.rule_for`` in ol-data-platform
    dg_projects/lakehouse/lakehouse/lib/openmetadata_domains.py, which this
    job cannot import: a rule matches a name when any of its shell-style globs
    does, case-insensitively, and the first matching rule wins.
    """

    rules: list[tuple[str | None, list[str]]]

    @classmethod
    def from_artifact(cls, artifact: dict[str, Any]) -> "DomainRules":
        """Read the rules the lakehouse code uploads as JSON."""
        return cls(
            rules=[(rule["domain"], rule["match"]) for rule in artifact["rules"]]
        )

    def match(self, name: str) -> tuple[bool, str | None]:
        """Return whether a rule matches *name*, and that rule's domain.

        :returns: ``(False, None)`` when no rule matches; ``(True, None)`` for a
            ``domain: null`` rule, which keeps the name out of every domain.
        """
        for domain, patterns in self.rules:
            if any(
                fnmatch.fnmatchcase(name.lower(), pattern.lower())
                for pattern in patterns
            ):
                return True, domain
        return False, None


def dbt_relations(manifest: dict[str, Any]) -> set[RelationKey]:
    """Return the tables the dbt job sets domains on: nodes and sources."""
    relations = {
        relation_key(node["schema"], node["alias"] or node["name"])
        for node in manifest["nodes"].values()
        if node["resource_type"] in _TABLE_RESOURCE_TYPES
    }
    relations.update(
        relation_key(source["schema"], source["identifier"] or source["name"])
        for source in manifest["sources"].values()
    )
    return relations


def own_domain(entity: dict[str, Any]) -> str | None:
    """Return the domain set on the entity itself, not one inherited from a parent.

    :raises ValueError: If the entity carries more than one domain, which the
        server's default rules forbid.
    """
    names = [
        ref["name"] for ref in entity.get("domains") or [] if not ref.get("inherited")
    ]
    if len(names) > 1:
        msg = f"{entity['fullyQualifiedName']} has several domains: {names}"
        raise ValueError(msg)
    return names[0] if names else None


def upstream_nodes(lineage: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the nodes with an edge straight into the lineage's entity.

    :param lineage: ``GET /v1/lineage/{type}/{id}`` response, fetched with
        ``upstreamDepth=1``.
    """
    entity_id = lineage["entity"]["id"]
    nodes = {node["id"]: node for node in lineage.get("nodes") or []}
    return [
        nodes[edge["fromEntity"]]
        for edge in lineage.get("upstreamEdges") or []
        if edge["toEntity"] == entity_id and edge["fromEntity"] in nodes
    ]


@dataclass
class Report:
    """What one run did to one asset type."""

    outcomes: Counter[str] = field(default_factory=Counter)
    unassigned: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def skip(self, reason: str, label: str) -> None:
        """Count an asset left without a derived domain, naming it."""
        self.outcomes[reason] += 1
        self.unassigned[reason].append(label)


@dataclass(frozen=True)
class Change:
    """A domain to set on one entity."""

    collection: str
    entity_id: str
    label: str
    current: str | None
    wanted: str


def single_domain(
    domains: set[str], label: str, report: Report, *, reason_none: str
) -> str | None:
    """Return the one domain an asset reads from, or record why it has none.

    :param domains: Domains of the tables or datasets the asset reads.
    :param label: The asset's name, for the report.
    :param report: Where an asset with no single domain is recorded.
    :param reason_none: The report's reason when *domains* is empty.
    """
    if len(domains) == 1:
        return next(iter(domains))
    if domains:
        report.skip("spans several domains", f"{label} {sorted(domains)}")
    else:
        report.skip(reason_none, label)
    return None


def plan_change(
    collection: str, entity: dict[str, Any], wanted: str | None, report: Report
) -> Change | None:
    """Return the change that gives *entity* the *wanted* domain, if it needs one."""
    if wanted is None:
        return None
    current = own_domain(entity)
    if current == wanted:
        report.outcomes["unchanged"] += 1
        return None
    return Change(
        collection=collection,
        entity_id=entity["id"],
        label=entity["fullyQualifiedName"],
        current=current,
        wanted=wanted,
    )


def plan_tables(
    tables: Iterable[dict[str, Any]],
    dbt_owned: set[RelationKey],
    rules: DomainRules,
    schema_includes: list[str],
    report: Report,
) -> tuple[list[Change], dict[RelationKey, str | None]]:
    """Plan the rule domains of the lake tables dbt does not know.

    :param tables: Lake tables, each with its ``domains`` and ``databaseSchema``.
    :param dbt_owned: Tables the dbt job sets domains on.
    :param rules: The domain rules.
    :param schema_includes: Regexes of the schemas whose tables are assigned,
        matched from the start of the name and ignoring case, as OpenMetadata's
        schema filter does.
    :param report: Where outcomes are counted.
    :returns: The changes, and every table's domain once they are made.
    """
    changes: list[Change] = []
    domains: dict[RelationKey, str | None] = {}
    for table in tables:
        schema = table["databaseSchema"]["name"]
        key = relation_key(schema, table["name"])
        domains[key] = own_domain(table)
        if not any(re.match(regex, schema, re.IGNORECASE) for regex in schema_includes):
            continue
        if key in dbt_owned:
            report.outcomes["left to the dbt job"] += 1
            continue
        matched, wanted = rules.match(table["name"])
        if not matched:
            report.skip("no rule matches", table["fullyQualifiedName"])
            continue
        if wanted is None:
            report.outcomes["in no domain by rule"] += 1
            continue
        if change := plan_change("tables", table, wanted, report):
            changes.append(change)
            domains[key] = wanted
    return changes, domains


def plan_lineage_assets(
    collection: str,
    assets: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    table_domains: dict[RelationKey, str | None],
    dataset_domains: dict[str, set[str]],
    report: Report,
) -> tuple[list[Change], dict[str, set[str]]]:
    """Plan the domains of datasets or dashboards from what they read.

    :param collection: The API collection the assets are patched through.
    :param assets: Each asset with its upstream lineage.
    :param table_domains: Lake table domains, from :func:`plan_tables`.
    :param dataset_domains: For each dataset id, the domains it reads; empty
        when planning the datasets themselves.
    :param report: Where outcomes are counted.
    :returns: The changes, and for each asset id the domains it reads (its own
        domain, if it reads none), for the dashboards built on it.
    """
    changes: list[Change] = []
    reads: dict[str, set[str]] = {}
    for asset, lineage in assets:
        domains: set[str] = set()
        for node in upstream_nodes(lineage):
            if node["type"] == "table":
                if domain := table_domains.get(
                    fqn_relation_key(node["fullyQualifiedName"])
                ):
                    domains.add(domain)
            elif node["type"] == "dashboardDataModel":
                domains |= dataset_domains.get(node["id"], set())
        wanted = single_domain(
            domains,
            asset["fullyQualifiedName"],
            report,
            reason_none="reads no table with a domain",
        )
        if change := plan_change(collection, asset, wanted, report):
            changes.append(change)
        current = own_domain(asset)
        reads[asset["id"]] = domains or ({current} if current else set())
    return changes, reads


class OpenMetadataClient:
    """The few OpenMetadata REST calls this job makes."""

    def __init__(self, server_url: str, token: str) -> None:
        self._url = f"{server_url.rstrip('/')}/v1"
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._session.get(
            f"{self._url}/{path}", params=params, timeout=_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json()

    def list_all(
        self, collection: str, params: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        """Yield every entity of a collection, following the paging cursor."""
        after = None
        while True:
            page = self._get(
                collection,
                {**params, "limit": _PAGE_SIZE, **({"after": after} if after else {})},
            )
            yield from page["data"]
            after = page.get("paging", {}).get("after")
            if not after:
                return

    def upstream_lineage(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        """Return the entity's lineage one hop upstream."""
        return self._get(
            f"lineage/{entity_type}/{entity_id}",
            {"upstreamDepth": 1, "downstreamDepth": 0},
        )

    def set_domain(self, collection: str, entity_id: str, domain_id: str) -> None:
        """Replace the entity's domains with one."""
        response = self._session.patch(
            f"{self._url}/{collection}/{entity_id}",
            headers={"Content-Type": "application/json-patch+json"},
            json=[
                {
                    "op": "add",
                    "path": "/domains",
                    "value": [{"id": domain_id, "type": "domain"}],
                }
            ],
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()


def apply_changes(
    client: OpenMetadataClient,
    changes: list[Change],
    domain_ids: dict[str, str],
    report: Report,
    *,
    dry_run: bool,
) -> None:
    """Make each change, counting the ones that fail rather than raising."""
    for change in changes:
        domain_id = domain_ids.get(change.wanted)
        if domain_id is None:
            # The dbt job skips a domain OpenMetadata does not know the same way.
            report.skip(f"domain {change.wanted} missing in OpenMetadata", change.label)
            continue
        verb = "Would set" if dry_run else "Setting"
        replacing = f" (replacing {change.current})" if change.current else ""
        log.info("%s %s domain %s%s", verb, change.label, change.wanted, replacing)
        if dry_run:
            report.outcomes["set (dry run)"] += 1
            continue
        try:
            client.set_domain(change.collection, change.entity_id, domain_id)
        except requests.HTTPError as err:
            log.error("Setting the domain of %s failed: %s", change.label, err)  # noqa: TRY400
            report.outcomes["failed"] += 1
        else:
            report.outcomes["set"] += 1


def _log_report(asset_type: str, report: Report) -> None:
    log.info(
        "%s: %s",
        asset_type,
        ", ".join(f"{outcome}={n}" for outcome, n in sorted(report.outcomes.items()))
        or "none found",
    )
    for reason, labels in sorted(report.unassigned.items()):
        log.warning(
            "%s with no domain, %s (%d): %s",
            asset_type,
            reason,
            len(labels),
            ", ".join(sorted(labels)),
        )


def _read_json(s3: Any, bucket: str, key: str) -> dict[str, Any]:
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def _with_lineage(
    client: OpenMetadataClient, entity_type: str, assets: Iterable[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (asset, client.upstream_lineage(entity_type, asset["id"])) for asset in assets
    ]


def main() -> None:
    """Plan and set the domains of lake tables, Superset datasets and dashboards."""
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    dry_run = json.loads(os.environ["OM_DOMAINS_DRY_RUN"])
    lake_service = os.environ["OM_LAKE_SERVICE_NAME"]
    superset_service = os.environ["OM_SUPERSET_SERVICE_NAME"]
    bucket = os.environ["OM_DBT_BUCKET"]
    prefix = os.environ["OM_DBT_PREFIX"].rstrip("/")

    s3 = boto3.client("s3", region_name=os.environ["OM_AWS_REGION"])
    rules = DomainRules.from_artifact(
        _read_json(s3, bucket, f"{prefix}/{DOMAIN_RULES_ARTIFACT}")
    )
    dbt_owned = dbt_relations(_read_json(s3, bucket, f"{prefix}/manifest.json"))

    client = OpenMetadataClient(
        os.environ["OM_SERVER_URL"], os.environ["OM_BOT_JWT_TOKEN"]
    )
    domain_ids = {
        domain["name"]: domain["id"] for domain in client.list_all("domains", {})
    }

    tables = Report()
    lake_tables = (
        table
        for database in client.list_all("databases", {"service": lake_service})
        for table in client.list_all(
            "tables",
            {"database": database["fullyQualifiedName"], "fields": "domains"},
        )
    )
    table_changes, table_domains = plan_tables(
        lake_tables,
        dbt_owned,
        rules,
        json.loads(os.environ["OM_DBT_SCHEMA_INCLUDES"]),
        tables,
    )
    apply_changes(client, table_changes, domain_ids, tables, dry_run=dry_run)

    datasets = Report()
    dataset_changes, dataset_domains = plan_lineage_assets(
        "dashboard/datamodels",
        _with_lineage(
            client,
            "dashboardDataModel",
            client.list_all(
                "dashboard/datamodels",
                {"service": superset_service, "fields": "domains"},
            ),
        ),
        table_domains,
        {},
        datasets,
    )
    apply_changes(client, dataset_changes, domain_ids, datasets, dry_run=dry_run)

    dashboards = Report()
    dashboard_changes, _ = plan_lineage_assets(
        "dashboards",
        _with_lineage(
            client,
            "dashboard",
            client.list_all(
                "dashboards", {"service": superset_service, "fields": "domains"}
            ),
        ),
        table_domains,
        dataset_domains,
        dashboards,
    )
    apply_changes(client, dashboard_changes, domain_ids, dashboards, dry_run=dry_run)

    reports = {
        f"{lake_service} tables": tables,
        "Superset datasets": datasets,
        "Superset dashboards": dashboards,
    }
    for asset_type, report in reports.items():
        _log_report(asset_type, report)

    if any(report.outcomes["failed"] for report in reports.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
