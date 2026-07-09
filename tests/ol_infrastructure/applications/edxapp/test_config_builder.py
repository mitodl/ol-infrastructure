"""Tests for edxapp config builder defaults."""

from ol_infrastructure.applications.edxapp.config_builder import (
    build_base_general_config,
    build_general_config,
)


def test_base_features_include_squelch_pii_in_logs() -> None:
    """The base FEATURES dict should include SQUELCH_PII_IN_LOGS."""
    config = build_base_general_config()
    assert config["FEATURES"]["SQUELCH_PII_IN_LOGS"] is True


def test_base_features_include_disable_start_dates_compat() -> None:
    """Compatibility key remains available in FEATURES for legacy code paths."""
    config = build_base_general_config()
    assert config["FEATURES"]["DISABLE_START_DATES"] is False


def test_deployment_overrides_preserve_squelch_pii_in_logs() -> None:
    """Deployment merge should not drop SQUELCH_PII_IN_LOGS from FEATURES."""
    config = build_general_config("mitxonline")
    assert config["FEATURES"]["SQUELCH_PII_IN_LOGS"] is True


def test_base_config_disables_survey_report_banner() -> None:
    """Avoid admin URL reverse lookups in LMS template context processors."""
    config = build_base_general_config()
    assert config["SURVEY_REPORT_ENABLE"] is False


def test_base_config_has_footer_social_defaults() -> None:
    """Branding footer helpers expect container settings, not None."""
    config = build_base_general_config()
    assert config["SOCIAL_MEDIA_FOOTER_NAMES"] == []
    assert config["SOCIAL_MEDIA_FOOTER_DISPLAY"] == {}
    assert config["SOCIAL_MEDIA_FOOTER_ACE_URLS"] == {}


def test_base_config_sets_github_repo_root() -> None:
    """Set GITHUB_REPO_ROOT used by import/export paths."""
    config = build_base_general_config()
    assert config["GITHUB_REPO_ROOT"] == "/openedx/data"
