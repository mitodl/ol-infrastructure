from pathlib import Path

RED_HAT = "RedHat"
DEBIAN = "Debian"

DOCKER_COMPOSE_DIRECTORY = Path("/etc/docker/compose")
DEFAULT_DIRECTORY_MODE = 755


def normalize_cpu_arch(arch_specifier: str) -> str:
    """Normalize the string used for the CPU kernel architecture.

    Different systems will report the CPU architecture differently and many software
    downloads will expect one or the other formats.  This function allows us to have a
    single location for being able to map back and forth.

    :param arch_specifier: The CPU architecture string returned from commands such as
        `uname -p`
    :type arch_specifier: str

    :returns: The common specifier used for the given architecture.

    :rtype: str
    """
    return {"amd64": "amd64", "x86_64": "amd64", "i386": "386", "i686": "386"}[
        arch_specifier
    ]


def linux_family(distribution_name: str) -> str:
    """Map a linux distribution to the family that it belongs to (e.g. Debian, etc.).

    :param distribution_name: The name of the linux distribution (e.g. Ubuntu, Debian,
        Fedora, etc.)
    :type distribution_name: str

    :returns: The family that the linux distribution belongs to (e.g. Debian, RedHat,
              etc.)

    :rtype: str
    """
    return {
        "Ubuntu": DEBIAN,
        DEBIAN: DEBIAN,
        RED_HAT: RED_HAT,
        "Fedora": RED_HAT,
        "CentOS": RED_HAT,
    }[distribution_name]
