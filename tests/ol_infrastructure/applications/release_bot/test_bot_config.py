"""Tests for the release bot's environment-variable configuration loaders."""

import json

import bot_config
import pytest


@pytest.fixture(autouse=True)
def _no_overrides_in_the_environment(monkeypatch):
    monkeypatch.delenv("SLACK_USER_OVERRIDES", raising=False)


def test_slack_user_overrides_round_trip(monkeypatch):
    overrides = {"dsubak": "UTESTUSER01", "james@jkachel.com": "UTESTUSER03"}
    monkeypatch.setenv("SLACK_USER_OVERRIDES", json.dumps(overrides))

    assert bot_config.load_slack_user_overrides() == overrides


def test_absent_slack_user_overrides_is_not_an_error():
    """A bot image newer than its secret boots and resolves authors as before."""
    assert bot_config.load_slack_user_overrides() == {}


def test_blank_slack_user_overrides_is_not_an_error(monkeypatch):
    """Kubernetes hands through an empty secret value as an empty string."""
    monkeypatch.setenv("SLACK_USER_OVERRIDES", "   ")

    assert bot_config.load_slack_user_overrides() == {}


def test_malformed_slack_user_overrides_degrades_instead_of_crashing(monkeypatch):
    """Losing the mentions is bad; losing every other bot command is worse."""
    monkeypatch.setenv("SLACK_USER_OVERRIDES", "{not json")

    assert bot_config.load_slack_user_overrides() == {}


def test_slack_user_overrides_must_be_an_object(monkeypatch):
    monkeypatch.setenv("SLACK_USER_OVERRIDES", '["dsubak", "UTESTUSER01"]')

    assert bot_config.load_slack_user_overrides() == {}
