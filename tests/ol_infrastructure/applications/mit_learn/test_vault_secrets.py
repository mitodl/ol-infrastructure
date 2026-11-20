"""Tests for MIT Learn's Vault-backed variable description."""

import json

import pytest

from ol_infrastructure.applications.mit_learn.vault_secrets import secret_env_names

from .test_k8s_secrets import FIXTURE, STACKS


@pytest.mark.parametrize("stack", STACKS)
def test_secret_env_names_are_every_variable_the_stack_templates(stack):
    shape = STACKS[stack]
    templated = [
        name
        for config in json.loads(FIXTURE.read_text())[stack]["vso_secrets"]
        for name, _ in config["templates"]
    ]
    assert (
        secret_env_names(
            starrocks_mount="database-starrocks" if shape["starrocks"] else None,
            memory_enabled=shape["memory"],
        )
        == templated
    )


def test_conditional_secrets_add_only_their_own_variables():
    base = secret_env_names(starrocks_mount=None, memory_enabled=False)
    full = secret_env_names(starrocks_mount="database-starrocks", memory_enabled=True)
    assert sorted(set(full) - set(base)) == [
        "OL_LLM_MEMORY_EXCHANGE_CLIENT_ID",
        "OL_LLM_MEMORY_EXCHANGE_CLIENT_SECRET",
        "STARROCKS_PASSWORD",
        "STARROCKS_USER",
    ]
    assert set(base) < set(full)
