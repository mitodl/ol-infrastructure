"""Override and secrets.yaml contracts the local-dev app programs share."""

import functools
from typing import NamedTuple

import pytest

from tests.local_dev.program_render import (
    DEPLOYMENT,
    REPO,
    literal_env,
    one,
    render_program,
)


class App(NamedTuple):
    name: str
    deployment: str
    env_vars_key: str
    override_variable: str


APPS = [
    App("learn-ai", "learn-ai-app", "learn_ai:env_vars", "OPENAI_API_KEY"),
    App(
        "ol-analytics-api",
        "ol-analytics-api-app",
        "ol_analytics_api:env_vars",
        "STARROCKS_PASSWORD",
    ),
]
SECRET_VALUE = "s3cret-override"  # noqa: S105  # pragma: allowlist secret


@pytest.fixture(params=APPS, ids=[app.name for app in APPS])
def app(request: pytest.FixtureRequest) -> App:
    return request.param


def test_an_override_wins_and_only_its_name_is_logged(
    app, tmp_path, monkeypatch, capsys
):
    mocks = render_program(
        app.name,
        tmp_path,
        monkeypatch,
        config={app.env_vars_key: {app.override_variable: SECRET_VALUE}},
    )
    env = literal_env(one(mocks, DEPLOYMENT, app.deployment))
    assert env[app.override_variable] == SECRET_VALUE
    err = capsys.readouterr().err
    assert f"{app.env_vars_key} overrides: {app.override_variable}" in err
    assert SECRET_VALUE not in err


def test_a_local_only_secret_stops_the_local_program(app, tmp_path, monkeypatch):
    """A key deployed stacks do not read belongs in LOCAL_ENV_DEFAULTS, not here."""
    secrets = (REPO / "local-dev/apps" / app.name / "secrets.yaml").read_text()
    edited = secrets.replace("unset:\n", "unset:\n- LOCAL_ONLY_VARIABLE\n", 1)
    assert edited != secrets
    render = functools.partial(render_program, app.name, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=r"not deployed: LOCAL_ONLY_VARIABLE$"):
        render(app_files={"secrets.yaml": edited})


def test_a_leftover_app_env_local_yaml_is_named_as_unread(
    app, tmp_path, monkeypatch, capsys
):
    """The YAML apps' override file is inert here; the apply log must say so,
    naming the keys to move (names only: the values may be secrets).
    """
    render_program(
        app.name,
        tmp_path,
        monkeypatch,
        app_files={"configmaps/app-env.local.yaml": 'data:\n  X: "1"\n  Y: "s3cret"\n'},
    )
    err = capsys.readouterr().err
    assert "configmaps/app-env.local.yaml is not read: move X, Y under" in err
    assert app.env_vars_key in err
    assert "s3cret" not in err
