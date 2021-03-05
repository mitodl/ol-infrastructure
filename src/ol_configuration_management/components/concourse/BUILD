pex_binary(
    name="pyinfra-concourse",
    entry_point="sample_deploy.py",
    zip_safe=False,
    dependencies=[":templates"]
)

python_library(
    dependencies=[":templates"]
)

resources(
    name="templates",
    sources=["templates/*.j2"]
)

python_tests(
    name="tests",
)
