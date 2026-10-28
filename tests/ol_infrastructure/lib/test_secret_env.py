import pytest

from ol_infrastructure.lib.secret_env import (
    VaultSecret,
    env_names,
    local_secret_data,
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
    assert vso_template("443") == "443"
    assert _secret(A=("x",), B="lit").templates == {
        "A": '{{ get .Secrets "x" }}',
        "B": "lit",
    }


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
