"""Regression tests for registry-driven, read-only SOPS deployment tracing."""

import importlib.util
import sys
from dataclasses import replace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the extensionless script without invoking its CLI or external tools."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "where-is-my-pr"
    loader = SourceFileLoader("where_is_my_pr", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    monkeypatch.setattr(
        module, "_gh_json", Mock(side_effect=AssertionError("GitHub call"))
    )
    monkeypatch.setattr(
        module, "_fly_curl", Mock(side_effect=AssertionError("fly call"))
    )
    monkeypatch.setattr(module, "_fly_ready", Mock(return_value=True))
    return module


@pytest.fixture
def pr(script: ModuleType) -> object:
    """Create a merged infrastructure PR without secret values."""
    return script.PullRequest(
        owner="mitodl",
        repo="ol-infrastructure",
        number=5809,
        title="Update secrets",
        state="MERGED",
        merged=True,
        commit="a" * 40,
        merged_at="2026-09-10T14:09:13Z",
    )


@pytest.mark.parametrize(
    ("relative", "projects"),
    [
        (
            "concourse/operations.qa.yaml",
            {"applications/concourse/", "applications/release_bot/"},
        ),
        (
            "vault/secrets.qa.yaml",
            {"substructure/vault/secrets/", "applications/learn_ai/"},
        ),
        ("mitxonline/secrets.production.yaml", {"applications/mitxonline/"}),
        ("witan/secrets.qa.yaml", {"applications/witan/"}),
        ("pulumi/consul.qa.yaml", {"infrastructure/consul/"}),
        ("pulumi/mongodb_atlas.mitxonline.qa.yaml", {"applications/edxapp/"}),
        ("pulumi/mongodb_atlas.yaml", set()),
        ("pulumi/vault.operations.qa.yaml", set()),
        ("unmapped/secrets.qa.yaml", set()),
    ],
)
def test_registry_consumers(
    script: ModuleType, relative: str, projects: set[str]
) -> None:
    """Reverse registry entries, respecting credential overrides."""
    assert script._secret_projects(f"src/bridge/secrets/{relative}") == projects


@pytest.mark.parametrize(
    ("relative", "entry", "expected"),
    [
        ("fastly.yaml", "fastly.yaml", True),
        ("nested/fastly.yaml", "fastly.yaml", False),
        ("concourse/operations.qa.yaml", "concourse/", True),
        ("concourse-other/operations.qa.yaml", "concourse/", False),
        (
            "pulumi/mongodb_atlas.mitxonline.qa.yaml",
            "pulumi/mongodb_atlas.*.*.yaml",
            True,
        ),
        (
            "pulumi/mongodb_atlas.mitxonline/nested.qa.yaml",
            "pulumi/mongodb_atlas.*.*.yaml",
            False,
        ),
    ],
)
def test_registry_path_boundaries(
    *,
    script: ModuleType,
    relative: str,
    entry: str,
    expected: bool,
) -> None:
    """Do not let filename globs cross directory boundaries."""
    assert script._secret_entry_matches(relative, entry) is expected


def test_changed_files_pagination(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slurp paginated GitHub arrays into valid JSON for large secrets PRs."""
    api = Mock(
        return_value=[
            [{"filename": "src/bridge/secrets/witan/secrets.qa.yaml"}],
            [{"filename": "src/bridge/secrets/mitxonline/secrets.production.yaml"}],
        ]
    )
    monkeypatch.setattr(script, "_gh_json", api)
    assert script._changed_files("mitodl", "ol-infrastructure", 5809) == [
        "src/bridge/secrets/witan/secrets.qa.yaml",
        "src/bridge/secrets/mitxonline/secrets.production.yaml",
    ]
    api.assert_called_once_with(
        "api",
        "--paginate",
        "--slurp",
        "repos/mitodl/ol-infrastructure/pulls/5809/files",
    )


def test_shared_secret_has_multiple_consumers(script: ModuleType) -> None:
    """Shared secrets must not be assigned to just one pipeline."""
    projects = script._secret_projects("src/bridge/secrets/fastly.yaml")
    assert {
        "applications/mit_learn/",
        "applications/xpro/",
        "infrastructure/vector_log_proxy/",
    } <= projects


def test_new_registry_entry_needs_no_script_route(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new audited project immediately participates in routing."""
    monkeypatch.setitem(script.PROJECT_SECRETS, "applications/new_app/", ["new_app/"])
    assert script._secret_projects("src/bridge/secrets/new_app/settings.qa.yaml") == {
        "applications/new_app/"
    }


@pytest.mark.parametrize(
    "path",
    [
        "src/bridge/secrets/sops.py",
        "src/bridge/secrets/__init__.py",
        "src/bridge/secrets/bin/rotate",
        "README.md",
        "src/bridge/secrets/concourse/operations.qa.yaml.bak",
    ],
)
def test_non_secret_paths(script: ModuleType, path: str) -> None:
    """Keep helpers and unrelated files on their existing reporting paths."""
    assert not script._is_secrets_file(path)


@pytest.fixture
def config() -> dict[str, Any]:
    """Use arbitrary pipeline/job/resource names to rule out naming shortcuts."""
    return {
        "resources": [
            {
                "name": "infra",
                "type": "git",
                "source": {"uri": "git@github.com:mitodl/ol-infrastructure.git"},
            },
            {
                "name": "updater",
                "type": "pulumi-provisioner",
                "source": {
                    "source_dir": (
                        "checkout/src/ol_infrastructure/applications/mitxonline/"
                    ),
                    "action": "update",
                },
            },
        ],
        "jobs": [
            {
                "name": "inspect-qa",
                "plan": [
                    {"get": "checkout", "resource": "infra"},
                    {
                        "put": "update",
                        "resource": "updater",
                        "params": {"stack_name": "QA", "preview": True},
                    },
                ],
            },
            {
                "name": "apply-qa",
                "plan": [
                    {
                        "in_parallel": {
                            "steps": [
                                {
                                    "get": "checkout",
                                    "resource": "infra",
                                    "passed": ["inspect-qa"],
                                }
                            ]
                        }
                    },
                    {
                        "do": [
                            {
                                "put": "update",
                                "resource": "updater",
                                "params": {"stack_name": "QA"},
                            }
                        ]
                    },
                ],
            },
        ],
    }


def test_routes_come_from_config(script: ModuleType, config: dict[str, Any]) -> None:
    """Extract source checkout aliases, resource aliases, stack and preview gates."""
    routes = script._routes_from_config("arbitrary-pipeline", config)
    assert len(routes) == 1
    route = routes[0]
    assert route.project == "applications/mitxonline/"
    assert route.stack == "QA"
    assert route.job == "apply-qa"
    assert route.resource == "infra"
    assert route.input_name == "checkout"
    assert route.previews == ("inspect-qa",)


@pytest.mark.parametrize("wrapper", ["try", "on_failure", "on_error", "ensure"])
def test_optional_updates_are_not_deploy_evidence(
    script: ModuleType,
    config: dict[str, Any],
    wrapper: str,
) -> None:
    """A successful job does not prove an optional/error-path update succeeded."""
    config["jobs"][1]["plan"][1] = {wrapper: config["jobs"][1]["plan"][1]}
    assert script._routes_from_config("pipeline", config) == []


@pytest.mark.parametrize(
    "uri",
    [
        "https://github.com/another/repo",
        "https://github.com/mitodl/ol-infrastructure-fork",
    ],
)
def test_only_ol_infra_inputs_count(
    script: ModuleType, config: dict[str, Any], uri: str
) -> None:
    """Never compare an app-repository commit as if it came from ol-infrastructure."""
    config["resources"][0]["source"]["uri"] = uri
    assert script._routes_from_config("pipeline", config) == []


@pytest.mark.parametrize("action", ["destroy", "refresh", "preview"])
def test_non_updates_do_not_count(
    script: ModuleType, config: dict[str, Any], action: str
) -> None:
    """Only update puts, never refresh/destroy/preview, establish deploy evidence."""
    config["resources"][1]["source"]["action"] = action
    assert script._routes_from_config("pipeline", config) == []


@pytest.mark.parametrize("kind", ["concourse", "vault", "simple"])
def test_generated_pipeline_contracts(script: ModuleType, kind: str) -> None:
    """Exercise current dedicated and simple Pulumi generators, not just fake plans."""
    from ol_concourse.pipelines.infrastructure.concourse.pipeline import (  # noqa: PLC0415
        concourse_pipeline,
    )
    from ol_concourse.pipelines.infrastructure.simple_pulumi.pipeline import (  # noqa: PLC0415
        build_simple_pulumi_pipeline,
    )
    from ol_concourse.pipelines.infrastructure.vault.pipeline import (  # noqa: PLC0415
        vault_pipeline,
    )

    pipelines = {
        "concourse": concourse_pipeline(),
        "vault": vault_pipeline,
        "simple": build_simple_pulumi_pipeline("airbyte"),
    }
    routes = script._routes_from_config(
        "any-name", pipelines[kind].model_dump(mode="json", exclude_none=True)
    )
    project = {
        "concourse": "applications/concourse/",
        "vault": "substructure/vault/secrets/",
        "simple": "applications/airbyte/",
    }[kind]
    selected = [r for r in routes if r.project == project]
    assert {r.stack.rsplit(".", 1)[-1] for r in selected} == {"CI", "QA", "Production"}
    assert all(not r.job.startswith("preview-") for r in routes)
    if kind != "simple":
        assert all(r.previews for r in selected if not r.stack.endswith("CI"))


def test_environment_and_group_selection(script: ModuleType) -> None:
    """Separate mitx from mitx-staging and QA from CI/production."""
    routes = [
        script.SecretsRoute(
            pipeline="edx",
            project="applications/edxapp/",
            stack=f"{group}.{env}",
            job=f"apply-{group}-{env}",
            resource="infra",
            input_name="infra",
        )
        for group in ("mitx", "mitx-staging", "mitxonline")
        for env in ("CI", "QA", "Production")
    ]
    matches = script._routes_for_secret(
        "src/bridge/secrets/edxapp/mitx-staging.qa.yaml", routes
    )
    assert [r.stack for r in matches] == ["mitx-staging.QA"]
    # Shared file without environment/group qualifiers applies to every stack.
    assert script._routes_for_secret("src/bridge/secrets/fastly.yaml", routes) == routes
    without_staging_qa = [r for r in routes if r.stack != "mitx-staging.QA"]
    assert (
        script._routes_for_secret(
            "src/bridge/secrets/edxapp/mitx-staging.qa.yaml", without_staging_qa
        )
        == []
    )


def test_hyphenated_environment(script: ModuleType) -> None:
    """edx_notes uses hyphens rather than dots before the environment."""
    routes = [
        script.SecretsRoute(
            pipeline="notes",
            project="applications/edx_notes/",
            stack=f"mitx.{env}",
            job=f"apply-{env}",
            resource="infra",
            input_name="infra",
        )
        for env in ("CI", "QA", "Production")
    ]
    assert [
        r.stack
        for r in script._routes_for_secret(
            "src/bridge/secrets/edx_notes/mitx-qa.yaml", routes
        )
    ] == ["mitx.QA"]


def test_discovery_and_instance_urls(
    script: ModuleType,
    config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discover arbitrary names and carry instance selectors through API/UI URLs."""
    api = Mock(
        side_effect=[
            [{"name": "new-pipeline", "instance_vars": {"branch": "feature/x"}}],
            {"config": config},
        ]
    )
    monkeypatch.setattr(script, "_fly_curl", api)
    routes, failures = script._discover_pulumi_routes()
    assert not failures
    assert len(routes) == 1
    route = routes[0]
    assert route.instance_query == "?vars.branch=%22feature%2Fx%22"
    assert api.call_args.args[0].endswith(
        "/new-pipeline/config?vars.branch=%22feature%2Fx%22"
    )
    assert route.job_url(route.job, "126").endswith(
        "/jobs/apply-qa/builds/126?vars.branch=%22feature%2Fx%22"
    )


@pytest.mark.parametrize("response", [None, {"error": "unauthorized"}])
def test_discovery_failures_are_explicit(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
) -> None:
    """An unavailable API must not be confused with absence of consumers."""
    monkeypatch.setattr(script, "_fly_curl", Mock(return_value=response))
    routes, failures = script._discover_pulumi_routes()
    assert not routes
    assert failures == ["pipeline list unavailable"]
    routes, failure = script._read_pipeline_routes({"name": "unavailable"})
    assert not routes
    assert failure == "unavailable"


@pytest.mark.parametrize("reached", [True, False, None])
def test_report_uses_live_deploy_evidence(  # noqa: PLR0913
    *,
    script: ModuleType,
    pr: object,
    config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reached: bool | None,
) -> None:
    """Reuse successful-build ancestry and report a shared deploy just once."""
    routes = script._routes_from_config("app", config)
    monkeypatch.setattr(
        script, "_discover_pulumi_routes", Mock(return_value=(routes, []))
    )
    live = Mock(return_value=script.LiveStage(reached, "42", None, "b" * 40))
    monkeypatch.setattr(script, "_live_stage", live)
    script._report_sops_secrets(
        pr,
        [
            "src/bridge/secrets/mitxonline/secrets.qa.yaml",
            "src/bridge/secrets/mitxonline/extra.qa.json",
        ],
    )
    assert [c.args for c in live.call_args_list] == [
        (pr, "app", job, ["checkout", "infra"], "")
        for job in ("inspect-qa", "apply-qa")
    ]
    output = capsys.readouterr().out
    assert "extra.qa.json" in output
    assert "SOPS consumer: applications/mitxonline/ QA" in output
    assert "/jobs/apply-qa/builds/42" in output
    assert "not proof a key was written to Vault" in output
    assert (
        "?  Deploy"
        if reached is None
        else "✓  Deploy"
        if reached
        else "not yet reached"
    ) in output


def test_credential_diagnostics_do_not_echo_registry_values(
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Use registry membership for classification, not its values for output."""
    monkeypatch.setattr(
        script,
        "DEPLOY_CREDENTIAL_SECRETS",
        {
            "pulumi/vault.*.*.yaml": "do-not-echo-description",
        },
    )
    script._report_sops_secrets(
        pr, ["src/bridge/secrets/pulumi/vault.operations.qa.yaml"]
    )
    output = capsys.readouterr().out
    assert "Provider credentials only" in output
    assert "do-not-echo-description" not in output


def test_failed_discovery_does_not_echo_instance_values(
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep instance values in the API request, not in failure diagnostics."""
    api = Mock(
        side_effect=[
            [
                {
                    "name": "unavailable",
                    "instance_vars": {"branch": "do-not-log-selector"},
                }
            ],
            None,
        ]
    )
    monkeypatch.setattr(script, "_fly_curl", api)
    script._report_sops_secrets(pr, ["src/bridge/secrets/mitxonline/secrets.qa.yaml"])
    output = capsys.readouterr().out
    assert "do-not-log-selector" in api.call_args.args[0]
    assert "Could not inspect unavailable" in output
    assert "discovery is incomplete" in output
    assert "do-not-log-selector" not in output


def test_credentials_unknown_and_logged_out(
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explain credential-only and unregistered files even without Concourse access."""
    monkeypatch.setattr(script, "_fly_ready", Mock(return_value=False))
    script._report_sops_secrets(
        pr,
        [
            "src/bridge/secrets/pulumi/vault.operations.qa.yaml",
            "src/bridge/secrets/unknown/secrets.qa.yaml",
            "src/bridge/secrets/witan/secrets.qa.yaml",
        ],
    )
    output = capsys.readouterr().out
    assert "Provider credentials only" in output
    assert "No registered Pulumi consumer" in output
    assert "applications/witan/" in output
    assert "fly -t infrastructure login" in output
    assert "✓" not in output


def test_missing_build_and_discovery_are_unknown(
    script: ModuleType,
    pr: object,
    config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep unknown states visible during partial discovery or build API failures."""
    routes = script._routes_from_config("app", config)
    monkeypatch.setattr(
        script,
        "_discover_pulumi_routes",
        Mock(return_value=(routes, ["unavailable-pipeline"])),
    )
    monkeypatch.setattr(script, "_live_stage", Mock(return_value=None))
    script._report_sops_secrets(
        pr,
        [
            "src/bridge/secrets/mitxonline/secrets.qa.yaml",
            "src/bridge/secrets/witan/secrets.qa.yaml",
        ],
    )
    output = capsys.readouterr().out
    assert "discovery is incomplete" in output
    assert "no matching deploy discovered for applications/witan/" in output
    assert "Could not verify a successful deploy" in output
    assert "✓" not in output


def test_mixed_pr_keeps_existing_reports(
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Retain app-code and unknown-file reporting alongside secret paths."""
    monkeypatch.setattr(
        script,
        "_changed_files",
        Mock(
            return_value=[
                "src/bridge/secrets/concourse/operations.qa.yaml",
                "src/ol_infrastructure/applications/mitxonline/__main__.py",
                "README.md",
            ]
        ),
    )
    monkeypatch.setattr(script, "_fly_ready", Mock(return_value=False))
    app_report = Mock()
    monkeypatch.setattr(script, "_report_ol_infra_app", app_report)
    script._report_ol_infrastructure_pr(pr)
    app_report.assert_called_once_with(pr, "mitxonline")
    output = capsys.readouterr().out
    assert "applications/concourse/" in output
    assert (
        "Other changed files (no known deployment pipeline mapping):\n    README.md"
        in output
    )


@pytest.mark.parametrize(
    ("path", "project"),
    [
        (
            "src/ol_infrastructure/applications/ocw_site/__main__.py",
            "applications/ocw_site/",
        ),
        (
            "src/ol_infrastructure/applications/ocw_site/snippets/redirects.vcl",
            "applications/ocw_site/",
        ),
        (
            "src/ol_infrastructure/infrastructure/vault/__main__.py",
            "infrastructure/vault/",
        ),
        ("src/ol_infrastructure/applications/ocw_site_extra/x.py", None),
        ("src/ol_infrastructure/lib/fastly.py", None),
        ("applications/ocw_site/__main__.py", None),
        ("README.md", None),
    ],
)
def test_pulumi_project_mapping(
    script: ModuleType, path: str, project: str | None
) -> None:
    """Map files to registered Pulumi projects on directory boundaries only."""
    assert script._pulumi_project(path) == project


def test_non_registry_project_is_traced(
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pulumi projects outside bridge.settings.apps use discovered deploy jobs."""
    changed = [
        "src/ol_infrastructure/applications/ocw_site/__main__.py",
        "src/ol_infrastructure/applications/ocw_site/snippets/redirects.vcl",
    ]
    monkeypatch.setattr(script, "_changed_files", Mock(return_value=changed))
    routes = [
        script.SecretsRoute(
            pipeline="pulumi-ocw-site",
            project="applications/ocw_site/",
            stack=stack,
            job=f"deploy-{stack}",
            resource="infra",
            input_name="infra",
            previews=(f"preview-{stack}",),
        )
        for stack in ("QA", "Production")
    ]
    unrelated = replace(routes[0], project="applications/ocw_studio/")
    discover = Mock(return_value=([*routes, unrelated], []))
    monkeypatch.setattr(script, "_discover_pulumi_routes", discover)
    live = Mock(
        return_value=script.LiveStage(
            reached=True, build_name="7", end_time=None, deployed_commit="b" * 40
        )
    )
    monkeypatch.setattr(script, "_live_stage", live)
    app_report = Mock()
    monkeypatch.setattr(script, "_report_ol_infra_app", app_report)
    script._report_ol_infrastructure_pr(pr)
    app_report.assert_not_called()
    assert [c.args[2] for c in live.call_args_list] == [
        "preview-QA",
        "deploy-QA",
        "preview-Production",
        "deploy-Production",
    ]
    output = capsys.readouterr().out
    assert "No changed files map" not in output
    assert "Other changed files" not in output
    assert "Pulumi project: applications/ocw_site/ QA (pulumi-ocw-site)" in output
    assert "redirects.vcl" in output
    assert "/pipelines/pulumi-ocw-site/jobs/preview-QA" in output
    assert "ocw_studio" not in output


def test_discovery_is_shared_and_gaps_are_explicit(
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Discover pipelines once per run and report projects with no deploy job."""
    monkeypatch.setattr(
        script,
        "_changed_files",
        Mock(
            return_value=[
                "src/bridge/secrets/fastly.yaml",
                "src/ol_infrastructure/applications/ocw_site/__main__.py",
            ]
        ),
    )
    discover = Mock(return_value=([], ["broken-pipeline"]))
    monkeypatch.setattr(script, "_discover_pulumi_routes", discover)
    script._report_ol_infrastructure_pr(pr)
    discover.assert_called_once_with()
    output = capsys.readouterr().out
    assert "applications/ocw_site/: no Concourse deploy job discovered" in output
    assert output.count("Could not inspect broken-pipeline") == 2
    assert "✓" not in output


def test_non_registry_project_logged_out(
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explain how to get live evidence rather than silently skipping."""
    monkeypatch.setattr(script, "_fly_ready", Mock(return_value=False))
    script._report_pulumi_projects(
        pr,
        {"applications/ocw_site/": ["src/ol_infrastructure/applications/ocw_site/x"]},
    )
    output = capsys.readouterr().out
    assert "applications/ocw_site/" in output
    assert "fly -t infrastructure login" in output


@pytest.mark.parametrize(
    ("status", "expected"), [("ahead", True), ("behind", False), (None, None)]
)
def test_live_stage_compares_successful_input(
    *,
    script: ModuleType,
    pr: object,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
    expected: bool | None,
) -> None:
    """Compare the PR SHA with the successful build, not the latest failure."""
    curl = Mock(
        side_effect=[
            [
                {"id": 99, "name": "43", "status": "failed"},
                {"id": 98, "name": "42", "status": "succeeded"},
            ],
            {"inputs": [{"name": "infra", "version": {"ref": "b" * 40}}]},
        ]
    )
    monkeypatch.setattr(script, "_fly_curl", curl)
    compare = Mock(return_value=status)
    monkeypatch.setattr(script, "_compare_status", compare)
    live = script._live_stage(
        pr, "pipeline", "apply", ["infra"], "?vars.group=%22test%22"
    )
    assert live.reached is expected
    assert live.build_name == "42"
    compare.assert_called_once_with("mitodl", "ol-infrastructure", "a" * 40, "b" * 40)
    assert (
        curl.call_args_list[0]
        .args[0]
        .endswith("/builds?vars.group=%22test%22&limit=10")
    )
    assert curl.call_args.args == ("/api/v1/builds/98/resources",)


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        (
            {"type": "git", "source": {"uri": "https://github.com/mitodl/Repo.git"}},
            "mitodl/repo",
        ),
        (
            {"type": "git", "source": {"uri": "git@github.com:mitodl/repo"}},
            "mitodl/repo",
        ),
        (
            {
                "type": "github-release",
                "source": {"owner": "mitodl", "repository": "repo"},
            },
            "mitodl/repo",
        ),
        ({"type": "git", "source": {"uri": "https://gitlab.com/mitodl/repo"}}, None),
        ({"type": "registry-image", "source": {"repository": "mitodl/repo"}}, None),
    ],
)
def test_resource_github_repo(
    script: ModuleType, resource: dict[str, Any], expected: str | None
) -> None:
    """Identify GitHub sources without treating image repositories as code."""
    assert script._resource_github_repo(resource) == expected


@pytest.fixture
def upstream_pr(script: ModuleType) -> object:
    """Create a merged PR to a repo shipped as a release another pipeline consumes."""
    return script.PullRequest(
        owner="mitodl",
        repo="theme",
        number=211,
        title="Theme change",
        state="MERGED",
        merged=True,
        commit="a" * 40,
        merged_at="2026-10-02T14:54:41Z",
    )


@pytest.fixture
def upstream_config(config: dict[str, Any]) -> dict[str, Any]:
    """Release -> image build -> Pulumi deploy, alongside an unrelated chain."""
    config["resources"] += [
        {
            "name": "theme-release",
            "type": "github-release",
            "source": {"owner": "mitodl", "repository": "theme"},
        },
        {"name": "image", "type": "registry-image", "source": {}},
    ]
    build = {
        "name": "build-image",
        "plan": [{"get": "theme", "resource": "theme-release"}, {"put": "image"}],
    }
    # Feed the existing preview/apply jobs from the image build.
    for job in config["jobs"]:
        passed = ["build-image"] if job["name"] == "inspect-qa" else ["inspect-qa"]
        job["plan"].insert(0, {"get": "image", "passed": passed})
    unrelated = {"name": "unrelated", "plan": [{"get": "infra"}]}
    config["jobs"] = [build, *config["jobs"], unrelated]
    return config


def test_upstream_consumers_follow_passed_chain(
    script: ModuleType, upstream_pr: object, upstream_config: dict[str, Any]
) -> None:
    """Jobs fed through passed constraints consume the release; others don't."""
    found = script.PipelineConfig("arbitrary", "", upstream_config)
    (source,) = script._upstream_sources(upstream_pr, found)
    assert source.resource == "theme-release"
    assert source.direct == {"build-image"}
    assert source.consumers == {"build-image", "inspect-qa", "apply-qa"}
    assert "image" in source.produced


def test_trace_deploy_back_to_release(
    script: ModuleType,
    upstream_pr: object,
    upstream_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A deploy's image resolves to the release commit its image build used."""
    found = script.PipelineConfig("arbitrary", "?vars.x=%221%22", upstream_config)
    (source,) = script._upstream_sources(upstream_pr, found)
    release = {"id": "9", "tag": "v1"}
    release_entry = {
        "id": 500,
        "version": release,
        "metadata": [{"name": "commit_sha", "value": "b" * 40}],
    }
    responses = {
        "/builds/7/resources": {
            "inputs": [
                {"name": "checkout", "version": {"ref": "c" * 40}},
                {"name": "image", "version": {"digest": "sha256:d"}},
            ]
        },
        "/image/versions?vars.x=%221%22&filter=digest%3Asha256%3Ad": [
            {"id": 400, "version": {"digest": "sha256:d"}}
        ],
        "/image/versions/400/output_of?vars.x=%221%22": [
            {"id": 6, "job_name": "build-image", "status": "succeeded"}
        ],
        "/builds/6/resources": {"inputs": [{"name": "theme", "version": release}]},
        "/theme-release/versions?vars.x=%221%22&filter=id%3A9&filter=tag%3Av1": [
            release_entry
        ],
        "/theme-release/versions?vars.x=%221%22&limit=1": [release_entry],
    }

    def curl(path: str) -> Any:
        return next(v for k, v in responses.items() if path.endswith(k))

    monkeypatch.setattr(script, "_fly_curl", Mock(side_effect=curl))
    monkeypatch.setattr(
        script,
        "_latest_successful_build",
        Mock(return_value={"id": 7, "name": "12"}),
    )
    compare = Mock(return_value="ahead")
    monkeypatch.setattr(script, "_compare_status", compare)
    assert script._traced_version(source, 7, "apply-qa") == release
    assert script._version_commit(source, release) == ("b" * 40, "v1 @ bbbbbbbb")
    script._report_upstream_source(upstream_pr, source)
    output = capsys.readouterr().out
    assert "✓  Release" in output
    assert "✓  QA           reached (apply-qa #12 used theme v1 @ bbbbbbbb)" in output
    assert "/jobs/apply-qa/builds/12?vars.x=%221%22" in output
    compare.assert_called_with("mitodl", "theme", "a" * 40, "b" * 40)


def test_moving_tag_without_recorded_commit_is_unknown(
    script: ModuleType,
    upstream_pr: object,
    upstream_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never ask GitHub where a tag points now; it may have moved since deploy."""
    found = script.PipelineConfig("arbitrary", "", upstream_config)
    (source,) = script._upstream_sources(upstream_pr, found)
    release = {"id": "1", "tag": "latest"}
    monkeypatch.setattr(
        script, "_fly_curl", Mock(return_value=[{"id": 1, "version": release}])
    )
    assert script._version_commit(source, release) is None


def test_archived_pipelines_are_not_failures(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archived pipelines serve no config; skip them rather than flag them."""
    api = Mock(side_effect=[[{"name": "old", "archived": True}]])
    monkeypatch.setattr(script, "_fly_curl", api)
    assert script._pipeline_configs() == ([], [])
    api.assert_called_once()


def test_unconsumed_repo_keeps_registry_message(
    script: ModuleType,
    upstream_pr: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fall back to the known-app list when no live pipeline consumes the repo."""
    monkeypatch.setattr(script, "_fetch_pr", Mock(return_value=upstream_pr))
    monkeypatch.setattr(script, "_pipeline_configs", Mock(return_value=([], [])))
    script.where_is_my_pr("https://github.com/mitodl/theme/pull/211")
    assert (
        "No known deployment pipeline maps to mitodl/theme" in capsys.readouterr().out
    )


def test_trace_only_follows_this_inputs_producers(
    script: ModuleType,
    upstream_pr: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An artifact both branches produced is traced through this deploy's branch."""
    config = {
        "resources": [
            {
                "name": "theme-release",
                "type": "github-release",
                "source": {"owner": "mitodl", "repository": "theme"},
            },
            {"name": "image", "type": "registry-image", "source": {}},
        ],
        "jobs": [
            *(
                {
                    "name": f"build-{branch}",
                    "plan": [{"get": "theme-release"}, {"put": "image"}],
                }
                for branch in ("a", "b")
            ),
            {
                "name": "preview-a",
                "plan": [{"get": "image", "passed": ["build-a"]}],
            },
            {
                "name": "deploy-a",
                "plan": [{"get": "image", "passed": ["preview-a"]}],
            },
        ],
    }
    found = script.PipelineConfig("arbitrary", "", config)
    (source,) = script._upstream_sources(upstream_pr, found)
    image = {"digest": "sha256:same"}
    responses = {
        "/builds/9/resources": {"inputs": [{"name": "image", "version": image}]},
        "/image/versions?filter=digest%3Asha256%3Asame": [{"id": 4, "version": image}],
        # The other branch's build is listed first.
        "/image/versions/4/output_of": [
            {"id": 2, "job_name": "build-b", "status": "succeeded"},
            {"id": 1, "job_name": "build-a", "status": "succeeded"},
        ],
        "/builds/2/resources": {
            "inputs": [{"name": "theme-release", "version": {"tag": "v2"}}]
        },
        "/builds/1/resources": {
            "inputs": [{"name": "theme-release", "version": {"tag": "v1"}}]
        },
    }
    monkeypatch.setattr(
        script,
        "_fly_curl",
        Mock(
            side_effect=lambda path: next(
                v for k, v in responses.items() if path.endswith(k)
            )
        ),
    )
    assert script._traced_version(source, 9, "deploy-a") == {"tag": "v1"}


def test_unresolved_stage_keeps_build_link(
    script: ModuleType,
    upstream_pr: object,
    upstream_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A build whose source version can't be resolved is still linked."""
    found = script.PipelineConfig("arbitrary", "", upstream_config)
    (source,) = script._upstream_sources(upstream_pr, found)
    monkeypatch.setattr(
        script,
        "_latest_successful_build",
        Mock(return_value={"id": 7, "name": "12"}),
    )
    monkeypatch.setattr(script, "_traced_version", Mock(return_value=None))
    script._report_upstream_stage(upstream_pr, source, "QA", "apply-qa")
    output = capsys.readouterr().out
    assert "?  QA" in output
    assert "/jobs/apply-qa/builds/12" in output
