python_sources(name="concourse")

python_distribution(
    name="ol-concourse",
    dependencies=[
        "src/concourse",
        "src/concourse/lib",
    ],
    description="A Pythonic way to manage your Concourse pipelines",
    provides=python_artifact(
        name="ol-concourse",
        version="0.1.0",
    ),
)

python_distribution(
    name="ol-concourse-pipelines",
    dependencies=[
        "src/concourse",
        "src/concourse/lib",
        "src/concourse/pipelines/open_edx/mfe",
    ],
    description="A Pythonic way to manage your Concourse pipelines",
    provides=python_artifact(
        name="ol-concourse",
        version="0.1.0",
    ),
)
