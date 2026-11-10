"""StarRocks resource groups and query queue settings, rendered as SQL.

ADR 0012 decision 3 isolates interactive from batch traffic inside one cluster per
environment. StarRocks OSS 4.1 stubs multi-warehouse and CN groups, so resource
groups and the global query queue are the only isolation available. The rules
below are the ones the 4.1 FE enforces (``ResourceGroupAnalyzer`` and
``ResourceGroup.validateCpuParameters`` on ``branch-4.1``), checked here so that a
bad stack config fails at preview instead of halfway through an apply.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The FE accepts only these two in a classifier (SUPPORTED_QUERY_TYPES). INSERT
# is every statement planned as TQueryType.LOAD: INSERT, INSERT OVERWRITE and the
# INSERT half of a CTAS.
QueryType = Literal["select", "insert"]

# ResourceGroupClassifier.USE_ROLE_PATTERN, which is also what keeps these
# identifiers safe to interpolate into SQL.
_IDENTIFIER_PATTERN = r"^\w+$"

CPU_KNOBS = (
    "cpu_weight",
    "cpu_weight_percent",
    "exclusive_cpu_cores",
    "exclusive_cpu_percent",
)


class Classifier(BaseModel):
    """One ``(...)`` term of a resource group's ``TO`` clause.

    The FE matches ``role`` against the session's active roles, not every role
    the user could inherit, and picks the satisfied classifier with the highest
    weight: one point each for role and query_type (plus 0.1 / number of
    types). A role-and-type classifier therefore outranks a type-only one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str | None = Field(default=None, pattern=_IDENTIFIER_PATTERN)
    query_types: tuple[QueryType, ...] = ()

    @model_validator(mode="after")
    def _has_a_condition(self) -> Self:
        if self.role is None and not self.query_types:
            msg = "A classifier needs a role, a query_type, or both"
            raise ValueError(msg)
        return self

    def to_sql(self) -> str:
        """Render the classifier as it appears in CREATE RESOURCE GROUP.

        :returns: e.g. ``(role='app', query_type in ('select'))``
        :rtype: str
        """
        terms = []
        if self.role is not None:
            terms.append(f"role='{self.role}'")
        if self.query_types:
            types = ", ".join(f"'{query_type}'" for query_type in self.query_types)
            terms.append(f"query_type in ({types})")
        return "(" + ", ".join(terms) + ")"


class ResourceGroupLimits(BaseModel):
    """The per-environment limits of one resource group, from stack config.

    Byte and row limits apply per CN, not per query across the cluster.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_weight: int | None = Field(default=None, ge=0)
    cpu_weight_percent: int | None = Field(default=None, ge=0, le=100)
    # The FE also rejects a value >= the smallest CN's core count. It counts the
    # cores the CN reports, which is its cgroup CPU quota (the pod's CPU limit),
    # so that check cannot be made here.
    exclusive_cpu_cores: int | None = Field(default=None, ge=0)
    exclusive_cpu_percent: int | None = Field(default=None, ge=0, lt=100)
    mem_limit: float = Field(gt=0, le=1)
    concurrency_limit: int | None = Field(default=None, ge=0)
    big_query_mem_limit: int | None = Field(default=None, ge=0)
    big_query_scan_rows_limit: int | None = Field(default=None, ge=0)
    big_query_cpu_second_limit: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _exactly_one_cpu_knob(self) -> Self:
        positive = [knob for knob in CPU_KNOBS if (getattr(self, knob) or 0) > 0]
        if len(positive) != 1:
            msg = (
                f"Exactly one of {', '.join(CPU_KNOBS)} must be positive;"
                f" got {positive or 'none'}"
            )
            raise ValueError(msg)
        return self

    def properties(self) -> dict[str, str]:
        """Return the WITH (...) properties, omitting unset limits.

        :returns: property name to value, both as SQL string literals' contents
        :rtype: dict[str, str]
        """
        return {
            key: str(value) for key, value in self.model_dump(exclude_none=True).items()
        }


class ResourceGroup(BaseModel):
    """A named resource group: who lands in it and what it may use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=_IDENTIFIER_PATTERN)
    # The FE refuses a normal group with no classifiers.
    classifiers: tuple[Classifier, ...] = Field(min_length=1)
    limits: ResourceGroupLimits

    def create_sql(self) -> str:
        """Render the statement that creates or converges this group.

        ``OR REPLACE`` validates the new definition, then drops and re-creates
        the group under one FE write lock, so classification never sees it
        missing, a rejected definition leaves the old group in place, and
        classifier and limit changes both converge
        without the ALTER ... DROP (<classifier id>) bookkeeping. The 4.1
        grammar puts ``OR REPLACE`` after ``GROUP``.

        :returns: a single CREATE RESOURCE GROUP statement
        :rtype: str
        """
        classifiers = ",\n    ".join(c.to_sql() for c in self.classifiers)
        properties = ",\n    ".join(
            f'"{key}" = "{value}"' for key, value in self.limits.properties().items()
        )
        return (
            f"CREATE RESOURCE GROUP OR REPLACE {self.name}\n"
            f"TO\n    {classifiers}\n"
            f"WITH (\n    {properties}\n);"
        )

    def drop_sql(self) -> str:
        """Render the statement that removes this group.

        :returns: a single DROP RESOURCE GROUP statement
        :rtype: str
        """
        return f"DROP RESOURCE GROUP IF EXISTS {self.name};"


# GlobalVariable.java defaults on branch-4.1, restored when the settings are deleted.
_QUERY_QUEUE_DEFAULTS: dict[str, bool | int] = {
    "enable_query_queue_select": False,
    "enable_query_queue_load": False,
    "enable_group_level_query_queue": False,
    "query_queue_concurrency_limit": 0,
    "query_queue_pending_timeout_second": 300,
}


class QueryQueueSettings(BaseModel):
    """The global query queue variables, from stack config.

    These are SET GLOBAL variables, which the FE persists in its edit log.
    ``ALTER WAREHOUSE`` would be the per-warehouse equivalent, but it hits the
    OSS multi-warehouse stub.

    They assume the FE runs query queue v1 (``enable_query_queue_v2 = false``
    in fe.conf, set by applications/starrocks). Under v2, the 4.1 default,
    every group draws from one cluster-wide slot pool of CN count x CN cores:
    measured on 4.1.6 with one 4-core BE, an interactive SELECT behind four
    batch SELECTs waited 11.6s with the SELECT queue on and ran in its normal
    3s with it off. v2 also has no group-level limit, so with the group-level
    queue on a group's concurrency_limit was enforced nowhere.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Which statements go through the FE queue at all. A query that skips it
    # also skips the group-level queue below, so the CN enforces its group's
    # concurrency_limit by rejecting it ("Exceed concurrency limit") instead of
    # queueing it. Batch takes INSERTs, so a concurrency_limit on batch needs
    # the load queue on as well as the SELECT queue.
    enable_query_queue_select: bool
    enable_query_queue_load: bool
    # On, the FE holds a query while its group is at concurrency_limit, and the
    # CN skips its own check. Only the v1 queue (DefaultSlotSelectionStrategy)
    # reads group limits. Measured on 4.1.6 with concurrency_limit = 1: v1
    # queued the second query, v2 ran both side by side, and with this off the
    # CN rejected the second.
    enable_group_level_query_queue: bool
    # 0 is unlimited. A positive value caps queries across every group at once,
    # so batch queries holding the slots would queue interactive ones behind
    # them.
    query_queue_concurrency_limit: int = Field(ge=0)
    query_queue_pending_timeout_second: int = Field(gt=0)

    @staticmethod
    def _set_sql(values: dict[str, bool | int]) -> str:
        return "\n".join(
            f"SET GLOBAL {name} = {str(value).lower()};"
            for name, value in values.items()
        )

    def set_sql(self, *, fe_query_queue_v2: bool) -> str:
        """Render the SET GLOBAL statements for these settings.

        :param fe_query_queue_v2: the FE's enable_query_queue_v2, as the
            applications stack exports it
        :returns: one SET GLOBAL statement per variable
        :rtype: str
        :raises ValueError: if a queue is on while the FE runs query queue v2
        """
        queues_on = [
            name
            for name in (
                "enable_query_queue_select",
                "enable_query_queue_load",
                "enable_group_level_query_queue",
            )
            if getattr(self, name)
        ]
        if fe_query_queue_v2 and queues_on:
            msg = (
                f"{', '.join(queues_on)} assume query queue v1, but the FE runs v2;"
                " apply applications/starrocks first"
            )
            raise ValueError(msg)
        return self._set_sql(self.model_dump())

    @classmethod
    def reset_sql(cls) -> str:
        """Render the SET GLOBAL statements that restore the FE defaults.

        :returns: one SET GLOBAL statement per variable
        :rtype: str
        """
        return cls._set_sql(_QUERY_QUEUE_DEFAULTS)
