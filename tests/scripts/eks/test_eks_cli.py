"""Tests for the top-level EKS CLI."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

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


@pytest.fixture
def two_clusters(eks_module):
    """Two ClusterConfig fixtures covering common test cases."""
    return [
        eks_module.ClusterConfig(
            cluster_name="applications-qa",
            server="https://applications-qa.example.invalid",
            certificate_authority_data="ca1",
            admin_role_arn="arn:aws:iam::123456789012:role/applications-qa-admin",
        ),
        eks_module.ClusterConfig(
            cluster_name="data-production",
            server="https://data-production.example.invalid",
            certificate_authority_data="ca2",
            admin_role_arn="arn:aws:iam::123456789012:role/data-production-admin",
        ),
    ]


# ---------------------------------------------------------------------------
# build_kubeconfig — developer mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_kubeconfig_developer_mode_produces_operator_contexts_by_default(
    eks_module, two_clusters
):
    """Developer mode defaults to operator contexts only."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters, eks_module.AccessMode.DEVELOPER, None
    )

    context_names = [ctx["name"] for ctx in kubeconfig["contexts"]]
    assert context_names == ["applications-qa", "data-production"]


@pytest.mark.unit
def test_build_kubeconfig_developer_mode_can_include_readonly_contexts(
    eks_module, two_clusters
):
    """Developer mode can optionally include paired readonly contexts."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters,
        eks_module.AccessMode.DEVELOPER,
        None,
        include_readonly_contexts=True,
    )

    context_names = [ctx["name"] for ctx in kubeconfig["contexts"]]
    assert context_names == [
        "applications-qa",
        "applications-qa-readonly",
        "data-production",
        "data-production-readonly",
    ]


@pytest.mark.unit
def test_build_kubeconfig_developer_operator_context_uses_developer_exec(
    eks_module, two_clusters
):
    """The operator context's exec args should include --mode developer."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters, eks_module.AccessMode.DEVELOPER, None
    )

    operator_user = next(
        u for u in kubeconfig["users"] if u["name"] == "applications-qa-developer"
    )
    args = operator_user["user"]["exec"]["args"]
    assert "--mode" in args
    assert args[args.index("--mode") + 1] == "developer"
    assert "--admin-role-arn" not in args


@pytest.mark.unit
def test_build_kubeconfig_developer_readonly_context_uses_readonly_exec(
    eks_module, two_clusters
):
    """The readonly context's exec args should include --mode readonly."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters,
        eks_module.AccessMode.DEVELOPER,
        None,
        include_readonly_contexts=True,
    )

    readonly_user = next(
        u for u in kubeconfig["users"] if u["name"] == "applications-qa-readonly"
    )
    args = readonly_user["user"]["exec"]["args"]
    assert "--mode" in args
    assert args[args.index("--mode") + 1] == "readonly"


@pytest.mark.unit
def test_load_valid_vault_token_rechecks_cache_after_lock(eks_module, monkeypatch):
    """A concurrent cache refresh should be reused instead of starting another login."""
    observed_tokens = iter([None, "cached-token"])

    monkeypatch.setattr(
        eks_module,
        "cached_vault_token",
        lambda _mode: next(observed_tokens),
    )
    monkeypatch.setattr(eks_module, "cache_lock", lambda _name: nullcontext())
    monkeypatch.setattr(
        eks_module,
        "oidc_login",
        lambda _client, _role: pytest.fail(
            "oidc_login should not run after cache refill"
        ),
    )

    assert (
        eks_module.load_valid_vault_token(eks_module.AccessMode.DEVELOPER)
        == "cached-token"
    )


@pytest.mark.unit
def test_load_valid_vault_token_refuses_noninteractive_login(eks_module, monkeypatch):
    """Non-interactive exec clients should not trigger browser-based OIDC login."""
    monkeypatch.setattr(eks_module, "cached_vault_token", lambda _mode: None)
    monkeypatch.setattr(eks_module, "cache_lock", lambda _name: nullcontext())
    monkeypatch.setattr(eks_module, "exec_invocation_is_interactive", lambda: False)
    monkeypatch.setattr(
        eks_module,
        "oidc_login",
        lambda _client, _role: pytest.fail(
            "oidc_login should not run for non-interactive exec callers"
        ),
    )

    with pytest.raises(RuntimeError, match="non-interactive"):
        eks_module.load_valid_vault_token(eks_module.AccessMode.DEVELOPER)


@pytest.mark.unit
def test_exec_invocation_is_interactive_uses_exec_info(eks_module, monkeypatch):
    """KUBERNETES_EXEC_INFO should control interactive prompting behavior."""
    monkeypatch.setenv(
        "KUBERNETES_EXEC_INFO",
        '{"apiVersion":"client.authentication.k8s.io/v1beta1","spec":{"interactive":false}}',
    )
    assert eks_module.exec_invocation_is_interactive() is False

    monkeypatch.setenv(
        "KUBERNETES_EXEC_INFO",
        '{"apiVersion":"client.authentication.k8s.io/v1beta1","spec":{"interactive":true}}',
    )
    assert eks_module.exec_invocation_is_interactive() is True


@pytest.mark.unit
def test_append_exec_debug_event_is_disabled_by_default(
    eks_module, monkeypatch, tmp_path
):
    """Debug logging should be opt-in so normal runs do not create a log file."""
    monkeypatch.setattr(eks_module, "EXEC_DEBUG_LOG_PATH", tmp_path / "exec-debug.log")
    monkeypatch.delenv(eks_module.EXEC_DEBUG_ENV_VAR, raising=False)

    eks_module.append_exec_debug_event("test-event")

    assert not eks_module.EXEC_DEBUG_LOG_PATH.exists()


@pytest.mark.unit
def test_build_kubeconfig_developer_current_context_is_operator(
    eks_module, two_clusters
):
    """Default current-context should be the operator context, not the readonly one."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters, eks_module.AccessMode.DEVELOPER, None
    )
    # PREFERRED_DEFAULT_CONTEXT is "applications-qa"
    assert kubeconfig["current-context"] == "applications-qa"


# ---------------------------------------------------------------------------
# build_kubeconfig — admin mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_kubeconfig_admin_mode_produces_operator_contexts_by_default(
    eks_module, two_clusters
):
    """Admin mode defaults to admin contexts only."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters, eks_module.AccessMode.ADMIN, None
    )

    context_names = [ctx["name"] for ctx in kubeconfig["contexts"]]
    assert context_names == ["applications-qa", "data-production"]


@pytest.mark.unit
def test_build_kubeconfig_admin_mode_can_include_readonly_contexts(
    eks_module, two_clusters
):
    """Admin mode can optionally include paired readonly contexts."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters,
        eks_module.AccessMode.ADMIN,
        None,
        include_readonly_contexts=True,
    )

    context_names = [ctx["name"] for ctx in kubeconfig["contexts"]]
    assert context_names == [
        "applications-qa",
        "applications-qa-readonly",
        "data-production",
        "data-production-readonly",
    ]


@pytest.mark.unit
def test_build_kubeconfig_admin_operator_context_includes_admin_role(
    eks_module, two_clusters
):
    """The admin operator context should pass --admin-role-arn in exec args."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters, eks_module.AccessMode.ADMIN, "data-production"
    )

    assert kubeconfig["current-context"] == "data-production"
    admin_user = next(
        u for u in kubeconfig["users"] if u["name"] == "applications-qa-admin"
    )
    args = admin_user["user"]["exec"]["args"]
    assert "--admin-role-arn" in args
    assert "arn:aws:iam::123456789012:role/applications-qa-admin" in args


@pytest.mark.unit
def test_build_kubeconfig_admin_readonly_context_does_not_include_admin_role(
    eks_module, two_clusters
):
    """The readonly context paired with admin mode must not pass an admin role ARN."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters,
        eks_module.AccessMode.ADMIN,
        None,
        include_readonly_contexts=True,
    )

    readonly_user = next(
        u for u in kubeconfig["users"] if u["name"] == "applications-qa-readonly"
    )
    args = readonly_user["user"]["exec"]["args"]
    assert "--admin-role-arn" not in args
    assert args[args.index("--mode") + 1] == "readonly"


# ---------------------------------------------------------------------------
# build_kubeconfig — readonly mode (single contexts)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_kubeconfig_readonly_mode_produces_single_contexts(
    eks_module, two_clusters
):
    """Readonly mode: single context per cluster, no -readonly suffix."""
    kubeconfig = eks_module.build_kubeconfig(
        two_clusters, eks_module.AccessMode.READONLY, None
    )

    context_names = [ctx["name"] for ctx in kubeconfig["contexts"]]
    assert context_names == ["applications-qa", "data-production"]
    assert not any("-readonly" in name for name in context_names)


# ---------------------------------------------------------------------------
# resolve_current_context
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_current_context_falls_back_to_first_cluster(eks_module):
    """The first cluster should be used when the preferred default is absent."""
    clusters = [
        eks_module.ClusterConfig(
            cluster_name="operations-ci",
            server="https://operations-ci.example.invalid",
            certificate_authority_data="ca1",
            admin_role_arn="",
        ),
        eks_module.ClusterConfig(
            cluster_name="data-ci",
            server="https://data-ci.example.invalid",
            certificate_authority_data="ca2",
            admin_role_arn="",
        ),
    ]
    assert eks_module.resolve_current_context(clusters, None) == "operations-ci"


# ---------------------------------------------------------------------------
# fetch_admin_role_arn
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_admin_role_arn_returns_cluster_admin_entry(eks_module):
    """fetch_admin_role_arn should return the ARN matching the naming convention."""
    # The production admin role is truncated by Pulumi (IAM 64-char limit):
    # 'applications-production-eks-admin-role-<suffix>' would be 65 chars, so
    # Pulumi emits 'applications-production-eks-admi<suffix>' instead.
    admin_arn = (
        "arn:aws:iam::123456789012:role/ol-infrastructure/eks/applications-production/"
        "applications-production-eks-admi20241203204503342200000005"
    )
    developer_arn = (
        "arn:aws:iam::123456789012:role/eks-cluster-shared-developer-role-c79eb98"
    )
    creator_arn = (
        "arn:aws:iam::123456789012:role/ol-infrastructure/eks/shared/"
        "eks-cluster-creator-role-b2b132f"
    )
    user_arn = "arn:aws:iam::123456789012:user/tmacey"
    backup_arn = (
        "arn:aws:iam::123456789012:role/ol-infrastructure/eks/applications-production/"
        "backup/applications-production-eks-backup-role"
    )

    mock_eks = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {"accessEntries": [developer_arn, creator_arn, user_arn, backup_arn, admin_arn]}
    ]
    mock_eks.get_paginator.return_value = mock_paginator

    result = eks_module.fetch_admin_role_arn(mock_eks, "applications-production")
    assert result == admin_arn
    mock_eks.list_associated_access_policies.assert_not_called()


@pytest.mark.unit
def test_fetch_admin_role_arn_works_for_short_cluster_name(eks_module):
    """fetch_admin_role_arn also matches non-truncated names (short cluster names)."""
    admin_arn = (
        "arn:aws:iam::123456789012:role/ol-infrastructure/eks/data-qa/"
        "data-qa-eks-admin-role-20241203183334260200000005"
    )
    mock_eks = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{"accessEntries": [admin_arn]}]
    mock_eks.get_paginator.return_value = mock_paginator

    result = eks_module.fetch_admin_role_arn(mock_eks, "data-qa")
    assert result == admin_arn


@pytest.mark.unit
def test_fetch_admin_role_arn_raises_when_none_found(eks_module):
    """fetch_admin_role_arn should raise when no admin entry exists."""
    mock_eks = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{"accessEntries": []}]
    mock_eks.get_paginator.return_value = mock_paginator

    with pytest.raises(RuntimeError, match="No cluster admin role found"):
        eks_module.fetch_admin_role_arn(mock_eks, "empty-cluster")


# ---------------------------------------------------------------------------
# setup command
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_setup_writes_kubeconfig_with_operator_contexts_by_default(
    tmp_path, eks_module, monkeypatch, two_clusters
):
    """Setup in developer mode should write operator contexts by default."""
    output_path = tmp_path / "config"
    monkeypatch.setattr(
        eks_module, "fetch_all_cluster_configs", lambda _mode: two_clusters
    )

    eks_module.setup(
        mode=eks_module.AccessMode.DEVELOPER,
        current_context="applications-qa",
        output_path=output_path,
    )

    kubeconfig = yaml.safe_load(output_path.read_text())
    assert kubeconfig["current-context"] == "applications-qa"
    assert len(kubeconfig["clusters"]) == 2

    context_names = [ctx["name"] for ctx in kubeconfig["contexts"]]
    assert context_names == ["applications-qa", "data-production"]

    # Operator user uses developer exec
    developer_user = next(
        u for u in kubeconfig["users"] if u["name"] == "applications-qa-developer"
    )
    assert "developer" in developer_user["user"]["exec"]["args"]

    user_names = [user["name"] for user in kubeconfig["users"]]
    assert "applications-qa-readonly" not in user_names
