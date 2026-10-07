"""Which governance role may read or write which data lake layer.

One mapping, consumed by every layer that enforces lake access, so they cannot
disagree. Gravitino (the Iceberg REST catalog) and StarRocks ``GRANT``s
intersect: a user needs both, and when they differ the narrower one wins with
nothing to say which layer refused. See
docs/plans/gravitino-authorization-spec.md, decisions A2-A5.

The table is derived from the ``+grants`` blocks of ``src/ol_dbt/dbt_project.yml``
in ol-data-platform. Consumers render their own syntax from it; nothing here
knows about Gravitino privileges or StarRocks SQL.
"""

from enum import StrEnum

from ol_infrastructure.lib.aws.iam_helper import DATA_LAKE_STAGES


class LakeAccess(StrEnum):
    """What a role may do in a layer. ``write`` includes ``read``."""

    read = "read"
    write = "write"


# The role_keys values Keycloak emits for ol-starrocks-client. StarRocks roles
# and Gravitino groups and roles carry the same names.
GOVERNANCE_ROLES: tuple[str, ...] = (
    "ol_platform_admin",
    "ol_data_engineer",
    "ol_data_analyst",
    "ol_business_analyst",
    "ol_researcher",
    "ol_instructor",
)

# Roles taken out of GOVERNANCE_ROLES. The Gravitino reconciler only converges
# the roles it is told about, so deleting a role's entry alone leaves its
# Gravitino role with every privilege and its group binding, and Keycloak may
# still emit the name in role_keys. The reconciler strips a name listed here of
# both instead. Remove it once every environment has converged.
RETIRED_ROLES: tuple[str, ...] = ()

# Read and write on the whole catalog, per-developer dbt schemas included.
CATALOG_WIDE_WRITE_ROLES: tuple[str, ...] = ("ol_platform_admin", "ol_data_engineer")

# dbt creates this database itself; it has no bucket and no Pulumi resource.
DBT_CREATED_LAYERS: tuple[str, ...] = ("migration",)
DATA_LAKE_LAYERS: tuple[str, ...] = (*DATA_LAKE_STAGES, *DBT_CREATED_LAYERS)

# Grants are per layer, never catalog-wide: the catalog also holds every
# developer's dbt schemas, which contain production-derived data. No role here
# reads ``raw``, which is the ingestion landing layer and has no dbt grant.
# ol_researcher and ol_instructor hold nothing until a data owner names what they
# may read (A5).
GOVERNANCE_LAYER_ACCESS: dict[str, dict[str, LakeAccess]] = {
    "ol_data_analyst": {
        "staging": LakeAccess.read,
        "migration": LakeAccess.read,
        "intermediate": LakeAccess.read,
        "dimensional": LakeAccess.read,
        "mart": LakeAccess.read,
        "reporting": LakeAccess.read,
        "integrations": LakeAccess.read,
        "external": LakeAccess.read,
    },
    "ol_business_analyst": {
        "intermediate": LakeAccess.read,
        "dimensional": LakeAccess.read,
        "mart": LakeAccess.read,
        "reporting": LakeAccess.read,
        "external": LakeAccess.read,
    },
    "ol_researcher": {},
    "ol_instructor": {},
}


def layer_database(env_suffix: str, layer: str) -> str:
    """Return the Glue database (Iceberg namespace) holding a layer.

    :param env_suffix: The environment suffix, e.g. ``qa``.
    :param layer: A member of ``DATA_LAKE_LAYERS``.

    :returns: The database name.
    """
    if layer not in DATA_LAKE_LAYERS:
        msg = f"{layer!r} is not a data lake layer; expected one of {DATA_LAKE_LAYERS}"
        raise ValueError(msg)
    return f"ol_warehouse_{env_suffix}_{layer}"


def validate_governance_access() -> None:
    """Raise ``ValueError`` when the mapping names an unknown role or layer."""
    scoped = GOVERNANCE_LAYER_ACCESS.keys()
    overlap = scoped & set(CATALOG_WIDE_WRITE_ROLES)
    if overlap:
        msg = f"{sorted(overlap)} are catalog-wide and must not also be layer-scoped"
        raise ValueError(msg)
    retired = set(RETIRED_ROLES) & set(GOVERNANCE_ROLES)
    if retired:
        msg = f"{sorted(retired)} are both governance roles and retired"
        raise ValueError(msg)
    covered = scoped | set(CATALOG_WIDE_WRITE_ROLES)
    if covered != set(GOVERNANCE_ROLES):
        msg = (
            "Every governance role needs an entry, even an empty one: "
            f"{sorted(covered ^ set(GOVERNANCE_ROLES))}"
        )
        raise ValueError(msg)
    for role, layers in GOVERNANCE_LAYER_ACCESS.items():
        unknown = layers.keys() - set(DATA_LAKE_LAYERS)
        if unknown:
            msg = f"{role} is granted unknown layers {sorted(unknown)}"
            raise ValueError(msg)


validate_governance_access()
