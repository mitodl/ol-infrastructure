import pytest
from pydantic import ValidationError

from ol_infrastructure.components.services.vault import OLVaultRestartTarget
from ol_infrastructure.lib import secret_env
from ol_infrastructure.lib.secret_env import (
    Template,
    VaultSecret,
    env_names,
    local_secret_data,
    vault_k8s_secrets,
    vso_template,
)


def _secret(name="app", **env):
    return VaultSecret(
        name=name,
        kind="static",
        mount="secret-app",
        mount_type="kv-v1",
        path="p",
        env=env,
    )


def test_a_key_path_renders_get_or_index_and_text_passes_through():
    assert vso_template(("value",)) == '{{ get .Secrets "value" }}'
    assert vso_template(("fastly", "api_key")) == (
        '{{ index .Secrets "fastly" "api_key" }}'
    )
    assert vso_template(Template("443")) == "443"
    assert _secret(A=("x",), B=Template("lit")).templates == {
        "A": '{{ get .Secrets "x" }}',
        "B": "lit",
    }


@pytest.mark.parametrize("source", ["value", ()])
def test_a_bare_string_or_empty_key_path_is_refused(source):
    """A key written as "value" instead of ("value",) would deploy the word."""
    with pytest.raises(ValidationError):
        _secret(A=source)


def test_mount_type_is_required_for_static_and_refused_for_dynamic():
    with pytest.raises(ValueError, match="needs mount_type"):
        VaultSecret(name="s", kind="static", mount="m", path="p", env={})
    with pytest.raises(ValueError, match="needs mount_type"):
        VaultSecret(
            name="d", kind="dynamic", mount="m", mount_type="kv-v2", path="p", env={}
        )
    assert _secret().secret_name == "app-static-secret"  # noqa: S105  # pragma: allowlist secret


def test_a_variable_written_by_two_secrets_is_refused():
    with pytest.raises(ValueError, match="more than one secret: B"):
        env_names([_secret("one", A=("a",), B=("b",)), _secret("two", B=("b",))])


def test_local_data_follows_the_description_and_drops_unset_names():
    names = ["A", "B", "C"]
    assert local_secret_data(names, {"C": "3", "A": "1"}, ["B"]) == {
        "A": "1",
        "C": "3",
    }


def test_names_another_secret_supplies_are_accounted_for_and_left_out():
    assert local_secret_data(
        ["A", "B", "C"], {"A": "1"}, ["B"], supplied_elsewhere=["C"]
    ) == {"A": "1"}
    with pytest.raises(ValueError, match="listed twice: C;"):
        local_secret_data(
            ["A", "C"], {"A": "1", "C": "3"}, [], supplied_elsewhere=["C"]
        )


@pytest.mark.parametrize(
    ("values", "unset", "message"),
    [
        ({"A": "1"}, [], "missing: B;"),
        ({"A": "1", "B": "2"}, ["B"], "listed twice: B;"),
        ({"A": "1", "B": "2", "Z": "9"}, [], "not deployed: Z$"),
        ({"A": "1"}, ["B", "Z"], "not deployed: Z$"),
    ],
)
def test_local_data_must_account_for_every_name_once(values, unset, message):
    with pytest.raises(ValueError, match=message):
        local_secret_data(["A", "B"], values, unset)


def test_an_empty_section_counts_as_no_entries():
    assert local_secret_data(["A"], {"A": "1"}, None) == {"A": "1"}
    with pytest.raises(ValueError, match="missing: A;"):
        local_secret_data(["A"], None, None)


def test_unquoted_yaml_values_are_refused():
    with pytest.raises(ValueError, match=r"must be strings \(quote them\): A, 7$"):
        local_secret_data(["A", "B"], {"A": 443}, ["B", 7])


def _vso_configs(monkeypatch, secrets, **kwargs):
    """Return the config each VSO resource would be created with, by secret name."""
    monkeypatch.setattr(
        secret_env,
        "OLVaultK8SSecret",
        lambda name, resource_config, opts: resource_config,  # noqa: ARG005
    )
    created = vault_k8s_secrets(
        secrets,
        resource_prefix="ol-app-qa",
        namespace="app",
        labels={},
        vaultauth="app",
        **kwargs,
    )
    return dict(created)


def test_restart_targets_reach_only_the_secret_they_name(monkeypatch):
    targets = [OLVaultRestartTarget(kind="Deployment", name="app-app")]
    configs = _vso_configs(
        monkeypatch,
        [_secret("one", A=("a",)), _secret("two", B=("b",))],
        restart_targets={"two": targets},
    )

    assert configs["one-static-secret"].restart_targets is None
    assert configs["two-static-secret"].restart_targets == targets


def test_without_restart_targets_no_secret_gets_any(monkeypatch):
    configs = _vso_configs(monkeypatch, [_secret("one", A=("a",))])

    assert configs["one-static-secret"].restart_targets is None


def test_restart_targets_for_a_secret_not_described_are_refused(monkeypatch):
    """A misspelled name would otherwise leave the secret it meant without any."""
    with pytest.raises(ValueError, match=r"not described: tow$"):
        _vso_configs(
            monkeypatch,
            [_secret("two", B=("b",))],
            restart_targets={
                "tow": [OLVaultRestartTarget(kind="Deployment", name="app-app")]
            },
        )
