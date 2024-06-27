python_sources(
    name="concourse-pipelines",
    sources=["**/*.py"]
)

python_distribution(
    name="ol-concourse-pipelines",
    dependencies=[
        ":concourse-pipelines",
        "src/ol_concourse:ol-concourse",
    ],
    description="Concourse pipeline definitions used at MIT Open Learning",
    provides=python_artifact(
        name="ol-concourse-pipelines",
        version="0.1.0",
    )
)
