python_sources(sources=["**/*.py"], name="ol-infrastructure-source")

python_distribution(
    name="ol-infrastructure-package",
    description="The OL Infrastructure package for deploying infrastructure.",
    dependencies=[":ol-infrastructure-source"],
    provides=python_artifact(
        name="ol_infrastructure",
        version="0.1.0",
    ),
)
