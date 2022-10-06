import pytest


@pytest.mark.caddy
def test_caddy_setup(host):
    caddy_user = host.user("caddy")
    assert caddy_user.exists  # noqa: S101
    assert caddy_user.shell in {"/bin/false", "/usr/sbin/nologin"}  # noqa: S101
    config_file = host.file("/etc/caddy/Caddyfile")
    assert config_file.exists  # noqa: S101
    assert config_file.is_file  # noqa: S101
    caddy_service = host.service("caddy")
    if caddy_service.is_enabled:
        assert caddy_service.is_running  # noqa: S101
