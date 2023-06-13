python_sources(
    name="concourse",
)

python_distribution(
    name="ol-concourse",
    dependencies=[
        ":concourse",
        "./lib:concourse-libs",
    ],
    description="A Pythonic way to manage your Concourse pipelines",
    interpreter_constraints=[">=3.8"],
    provides=python_artifact(
        name="ol-concourse",
        version="0.4.1",
    ),
    repositories=["@pypi"]
)
