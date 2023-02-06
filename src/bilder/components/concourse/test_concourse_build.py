import pytest


@pytest.mark.concourse
def test_concourse_installed(host):
    concourse_user = host.user("concourse")
    assert concourse_user.exists
    assert concourse_user.shell == "/bin/false"
    app_dir = host.file("/opt/concourse")
    assert app_dir.exists
    assert app_dir.is_directory
    assert app_dir.user == concourse_user.name
    assert host.file("/etc/default/concourse").exists
    concourse = host.service("concourse")
    if concourse.is_enabled:
        assert concourse.is_running
