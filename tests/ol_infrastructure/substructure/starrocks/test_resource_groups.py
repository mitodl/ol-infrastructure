"""Resource group config must fail at preview where the 4.1 FE would refuse it.

A rejected CREATE RESOURCE GROUP fails the apply after the query queue settings
have already changed, so the FE's own rules are checked on the stack config.
"""

import pytest
from pydantic import ValidationError

from ol_infrastructure.substructure.starrocks.resource_groups import (
    Classifier,
    QueryQueueSettings,
    ResourceGroup,
    ResourceGroupLimits,
)


def test_limits_require_exactly_one_cpu_knob() -> None:
    with pytest.raises(ValidationError, match="Exactly one of"):
        ResourceGroupLimits(mem_limit=0.5)
    with pytest.raises(ValidationError, match="Exactly one of"):
        ResourceGroupLimits(mem_limit=0.5, cpu_weight=2, exclusive_cpu_cores=1)


def test_zero_cpu_knobs_do_not_count_as_set() -> None:
    limits = ResourceGroupLimits(mem_limit=0.5, cpu_weight=0, exclusive_cpu_cores=2)

    assert limits.properties()["exclusive_cpu_cores"] == "2"


@pytest.mark.parametrize("mem_limit", [0, 1.5])
def test_mem_limit_is_a_fraction(mem_limit: float) -> None:
    with pytest.raises(ValidationError):
        ResourceGroupLimits(mem_limit=mem_limit, cpu_weight=1)


def test_classifier_needs_a_condition() -> None:
    with pytest.raises(ValidationError, match="needs a role"):
        Classifier()


def test_classifier_role_cannot_carry_sql() -> None:
    with pytest.raises(ValidationError):
        Classifier(role="app'), (role='admin")


def test_create_sql_renders_classifiers_and_only_set_properties() -> None:
    group = ResourceGroup(
        name="interactive",
        classifiers=(
            Classifier(role="app", query_types=("select",)),
            Classifier(query_types=("insert",)),
        ),
        limits=ResourceGroupLimits(
            exclusive_cpu_cores=3, mem_limit=0.4, concurrency_limit=20
        ),
    )

    assert group.create_sql() == (
        "CREATE RESOURCE GROUP OR REPLACE interactive\n"
        "TO\n"
        "    (role='app', query_type in ('select')),\n"
        "    (query_type in ('insert'))\n"
        "WITH (\n"
        '    "exclusive_cpu_cores" = "3",\n'
        '    "mem_limit" = "0.4",\n'
        '    "concurrency_limit" = "20"\n'
        ");"
    )
    assert group.drop_sql() == "DROP RESOURCE GROUP IF EXISTS interactive;"


def test_resource_group_needs_a_classifier() -> None:
    with pytest.raises(ValidationError):
        ResourceGroup(
            name="batch",
            classifiers=(),
            limits=ResourceGroupLimits(mem_limit=0.6, cpu_weight=1),
        )


def test_query_queue_sql_sets_and_resets_every_variable() -> None:
    settings = QueryQueueSettings(
        enable_query_queue_select=True,
        enable_group_level_query_queue=True,
        query_queue_concurrency_limit=0,
        query_queue_pending_timeout_second=120,
    )

    assert settings.set_sql() == (
        "SET GLOBAL enable_query_queue_select = true;\n"
        "SET GLOBAL enable_group_level_query_queue = true;\n"
        "SET GLOBAL query_queue_concurrency_limit = 0;\n"
        "SET GLOBAL query_queue_pending_timeout_second = 120;"
    )
    assert QueryQueueSettings.reset_sql() == (
        "SET GLOBAL enable_query_queue_select = false;\n"
        "SET GLOBAL enable_group_level_query_queue = false;\n"
        "SET GLOBAL query_queue_concurrency_limit = 0;\n"
        "SET GLOBAL query_queue_pending_timeout_second = 300;"
    )
