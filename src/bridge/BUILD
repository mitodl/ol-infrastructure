python_sources()

python_distribution(
    name="bridge-package",
    dependencies=[
        "src/bridge/secrets:sops_secrets",
        "src/bridge/secrets:secrets",
        "src/bridge/lib:lib",
    ],
    description="The OL Infrastructure bridge package for tying together configuration and deployment.",
    provides=python_artifact(
        name="bridge",
        version="0.1.0",
    )
)
