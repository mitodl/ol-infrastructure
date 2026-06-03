"""Tests for the top-level EKS CLI."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "eks" / "eks.py"


def load_eks_module():
    """Load the EKS CLI module directly from scripts/eks/eks.py."""
    spec = importlib.util.spec_from_file_location("test_scripts_eks_cli", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        msg = f"Unable to load module from {SCRIPT_PATH}"
        raise RuntimeError(msg)

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def eks_module():
    """Return the loaded EKS CLI module."""
    return load_eks_module()


@pytest.mark.unit
def test_cluster_name_for_stack(eks_module):
    """Cluster names should be derived from Pulumi stack names."""
    assert eks_module.cluster_name_for_stack("applications.QA") == "applications-qa"
    assert eks_module.cluster_name_for_stack("operations.Production") == (
        "operations-production"
    )


@pytest.mark.unit
def test_read_stack_names_uses_current_eks_stack_files(
    tmp_path, eks_module, monkeypatch
):
    """Stack discovery should use sorted Pulumi stack config filenames."""
    for filename in [
        "Pulumi.operations.QA.yaml",
        "Pulumi.applications.CI.yaml",
        "Pulumi.data.Production.yaml",
    ]:
        (tmp_path / filename).write_text("config: {}\n")

    monkeypatch.setattr(eks_module, "PULUMI_EKS_PROJECT_DIR", tmp_path)

    assert eks_module.read_stack_names() == [
        "applications.CI",
        "data.Production",
        "operations.QA",
    ]


@pytest.mark.unit
def test_build_kubeconfig_for_readonly_mode(eks_module):
    """Readonly kubeconfigs should call the exec helper without admin role args."""
    cluster = eks_module.ClusterConfig(
        cluster_name="applications-qa",
        stack_name="applications.QA",
        server="https://example.invalid",
        certificate_authority_data="abc123",
        admin_role_arn="arn:aws:iam::123456789012:role/admin",
    )

    kubeconfig = eks_module.build_kubeconfig(
        [cluster], eks_module.AccessMode.READONLY, None
    )

    assert kubeconfig["clusters"][0]["name"] == "applications-qa"
    assert kubeconfig["contexts"][0]["name"] == "applications-qa"
    assert kubeconfig["current-context"] == "applications-qa"
    exec_config = kubeconfig["users"][0]["user"]["exec"]
    assert Path(exec_config["command"]).name == "uv"
    assert exec_config["args"][:5] == [
        "run",
        "--project",
        str(eks_module.repo_root()),
        "python",
        str(Path(eks_module.__file__).resolve()),
    ]
    assert "exec-credential" in exec_config["args"]
    assert "readonly" in exec_config["args"]
    assert "--admin-role-arn" not in exec_config["args"]


@pytest.mark.unit
def test_build_kubeconfig_for_admin_mode_includes_admin_role(eks_module):
    """Admin kubeconfigs should pass the cluster-specific admin role ARN."""
    cluster = eks_module.ClusterConfig(
        cluster_name="operations-production",
        stack_name="operations.Production",
        server="https://example.invalid",
        certificate_authority_data="xyz789",
        admin_role_arn="arn:aws:iam::123456789012:role/cluster-admin",
    )

    kubeconfig = eks_module.build_kubeconfig(
        [cluster], eks_module.AccessMode.ADMIN, "operations-production"
    )

    assert kubeconfig["current-context"] == "operations-production"
    exec_args = kubeconfig["users"][0]["user"]["exec"]["args"]
    assert "admin" in exec_args
    assert "--admin-role-arn" in exec_args
    assert "arn:aws:iam::123456789012:role/cluster-admin" in exec_args


@pytest.mark.unit
def test_resolve_current_context_falls_back_to_first_cluster(eks_module):
    """The first cluster should be used when the preferred default is absent."""
    clusters = [
        eks_module.ClusterConfig(
            cluster_name="operations-ci",
            stack_name="operations.CI",
            server="https://operations-ci.example.invalid",
            certificate_authority_data="ca1",
            admin_role_arn="arn:aws:iam::123456789012:role/admin-1",
        ),
        eks_module.ClusterConfig(
            cluster_name="data-ci",
            stack_name="data.CI",
            server="https://data-ci.example.invalid",
            certificate_authority_data="ca2",
            admin_role_arn="arn:aws:iam::123456789012:role/admin-2",
        ),
    ]

    assert eks_module.resolve_current_context(clusters, None) == "operations-ci"


@pytest.mark.unit
def test_setup_writes_managed_kubeconfig(tmp_path, eks_module, monkeypatch):
    """Setup should write a full kubeconfig file to the requested path."""
    output_path = tmp_path / "config"
    clusters = [
        eks_module.ClusterConfig(
            cluster_name="applications-ci",
            stack_name="applications.CI",
            server="https://applications-ci.example.invalid",
            certificate_authority_data="ca1",
            admin_role_arn="arn:aws:iam::123456789012:role/admin-1",
        ),
        eks_module.ClusterConfig(
            cluster_name="data-qa",
            stack_name="data.QA",
            server="https://data-qa.example.invalid",
            certificate_authority_data="ca2",
            admin_role_arn="arn:aws:iam::123456789012:role/admin-2",
        ),
    ]

    monkeypatch.setattr(eks_module, "fetch_all_cluster_configs", lambda: clusters)

    eks_module.setup(
        mode=eks_module.AccessMode.DEVELOPER,
        current_context="data-qa",
        output_path=output_path,
    )

    kubeconfig = yaml.safe_load(output_path.read_text())
    assert kubeconfig["current-context"] == "data-qa"
    assert len(kubeconfig["clusters"]) == 2
    assert [context["name"] for context in kubeconfig["contexts"]] == [
        "applications-ci",
        "data-qa",
    ]
    assert kubeconfig["users"][1]["user"]["exec"]["args"][-1] == "developer"
