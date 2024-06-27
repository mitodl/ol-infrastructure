python_library(
    dependencies=[":templates"]
)

resources(
    name="templates",
    sources=["templates/*.j2"]
)

python_distribution(
    dependencies=[":templates"],
    setup_py_commands=["sdist", "bdist_wheel"]
)
