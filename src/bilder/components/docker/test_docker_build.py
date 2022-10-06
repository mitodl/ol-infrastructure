def test_docker_running_and_enabled(host):
    docker = host.service("docker")
    print(docker)  # noqa: WPS421
    assert docker.is_running  # noqa: S101
    assert docker.is_enabled  # noqa: S101
