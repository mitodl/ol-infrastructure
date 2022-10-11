import pytest


@pytest.mark.concourse
def test_concourse_installed(host):  # noqa: WPS218
    concourse_user = host.user("concourse")
    assert concourse_user.exists  # noqa: S101
    assert concourse_user.shell == "/bin/false"  # noqa: S101
    app_dir = host.file("/opt/concourse")
    assert app_dir.exists  # noqa: S101
    assert app_dir.is_directory  # noqa: S101
    assert app_dir.user == concourse_user.name  # noqa: S101
    assert host.file("/etc/default/concourse").exists  # noqa: S101
    concourse = host.service("concourse")
    if concourse.is_enabled:
        assert concourse.is_running  # noqa: S101
