python_sources(name="bridge", sources=["**/*.py"])

resources(
    name="sops_secrets",
    sources=[
        "./secrets/bin/*",
        "./secrets/**/*.yaml",
        "./secrets/**/*.json",
        "./secrets/**/*.env",
    ],
    description="Files containing encrypted values managed via SOPS for use with Pulumi and PyInfra code.",
)

python_distribution(
    name="bridge-package",
    dependencies=[
        ":sops_secrets",
        ":bridge",
    ],
    description="The OL Infrastructure bridge package for tying together configuration and deployment.",
    provides=python_artifact(
        name="bridge",
        version="0.2.0",
    ),
)
