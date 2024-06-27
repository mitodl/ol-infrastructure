def test_docker_running_and_enabled(host):
    docker = host.service("docker")
    print(docker)  # noqa: T201
    assert docker.is_running
    assert docker.is_enabled
