from pyinfra.operations import apt

apt.packages(
    name="Install baseline packages for Debian based hosts",
    packages=[
        "curl",
    ],
    update=True,
)
