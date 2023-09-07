import pytest


@pytest.mark.caddy()
def test_caddy_setup(host):
    caddy_user = host.user("caddy")
    assert caddy_user.exists
    assert caddy_user.shell in {"/bin/false", "/usr/sbin/nologin"}
    config_file = host.file("/etc/caddy/Caddyfile")
    assert config_file.exists
    assert config_file.is_file
    caddy_service = host.service("caddy")
    if caddy_service.is_enabled:
        assert caddy_service.is_running
