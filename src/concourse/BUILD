python_sources(name="concourse")

python_distribution(
    name="ol-concourse",
    dependencies=[
        "src/concourse:concourse",
        "src/concourse/lib:concourse-libs",
    ],
    description="A Pythonic way to manage your Concourse pipelines",
    provides=python_artifact(
        name="ol-concourse",
        version="0.1.0",
    ),
)
