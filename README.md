In order to streamline the management of infrastructure required to support the services
run by MIT Open Learning we have implemented our configuration management logic using
PyInfra. This has the benefit of being pure Python, allowing us to take advantage of the
broad ecosystem that it provides including linting, testing, and integrations.

## Project Structure
Any given service is unlikely to be composed of a single service, instead requiring a
variety of components to be used together. In order to make the composition of a running
system easier to reason about, we decompose the responsibilities for any given piece of
technology into a self-contained component. These components live under the
`src/components/` directory. Each component provide at minimum the logic needed to
install and configure the associated service. In addition, each component should also
provide the logic needed to upgrade an instance of the service, the SystemD unit files
needed to register the service, and any logic required to perform maintenance on the
associated technology.

## Configuration Objects
A common requirement across all configuration management systems is a method to provide
settings at build/runtime to populate config files, determine which logical paths to
take, etc. A problem that often comes from managing these settings is uncertainty around
the constraints for each value, and the ability to easily merge default values with user
supplied information. In order to reduce the complexity of this situation and provide
useful constraints we use [Pydantic](https://pydantic-docs.helpmanual.io/) models with
relevant validators to contain the values needed by each component.

Each model defined for a given component will inherit from
`ol_configuration_management.lib.model_helpers.OLBaseSettings`. This class inherits from
the Pydantic `BaseSettings` class and sets the `case_sensitive` model setting to
`False`. This allows us to easily use environment variables to override attributes of a
model at build time.
