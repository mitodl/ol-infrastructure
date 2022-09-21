# Overview
This repository is a monorepo for managing the configuration and deployment of services managed by MIT Open Learning engineering. It uses a combination of Pulumi and PyInfra to build a pure-python deployment stack to enable more developer-friendly access to creating and modifying the systems that power the applications that we build and serve.

All infrastructure provisioning performed via Pulumi is located under `src/ol-infrastructure/` and all configuration management written in PyInfra lives under `src/bilder`.

# Infrastructure Management

## Getting Started
This is a Pulumi project, so the first step is to install the Pulumi CLI along with the relevant language
protocol. Instructions [here](https://www.pulumi.com/docs/get-started/install/).

To install and manage the relevant dependencies this project uses [Poetry](https://python-poetry.org/). After installing
the Poetry CLI simply run `poetry install`.

We use the S3 state backend for Pulumi, so after installing the Pulumi CLI run `pulumi login s3://mitol-pulumi-state`.

## Structure

This is a monorepo of the Pulumi code that we use to manage the infrastructure that powers MIT Open Learning

Component resources are located under `src/ol-infrastructure/components/` with a descriptive name that makes it evident what the intent of the
component is.

Concrete implementations of infrastructure are located under `src/ol-infrastructure/infrastructure/` with a descriptive name that makes it
evident what is contained in that project.

Management of resources that rely on foundational infrastructure being provisioned, but
which supports the operation of applications is located under
`src/ol-infrastructure/substructure`. An Example of this includes Consul prepared queries.

Provisioning of all of the resources needed to support and deploy a specific application
is located under `src/ol-infrastructure/applications/` with a name that indicates the
application being managed (e.g. `concourse`).

Each component or concrete infrastructure that is more complex than a single resource will include a `diagram.py` file
that uses the [diagrams](https://diagrams.mingrammer.com/) package to illustrate the system structure that it creates.

## Nomenclature

Pulumi organizes code into `Projects` which represent a deployable unit. Within a project they have a concept of
`Stacks` which are often used as a mapping for different environments. Each module underneath `src/ol-infrastructure/infrastructure/` and
`src/ol-infrastructure/applications/` is its own `Project`, meaning that it will have a `Pulumi.yaml` definition of that project. Each `stack`
has its own yaml file in which the configuration for that stack is defined.

## Conventions

Stack names should be a dot-separated namespaced representation of the project path, suffixed with an environment
specifier in the form of QA or Production. The capitalization is important as it will be used directly to interpolate
into tag objects. It is easier to start with QA and Production and then call `.lower()` than it is to build a dictionary
mapping the lowercase versions to their properly capitalized representation.

The dotted namespace allows for peaceful coexistence of multiple projects within a single state backend, as well as
allowing for use of [stack references](https://www.pulumi.com/docs/tutorials/aws/aws-py-stackreference/) between
projects.

The infrastructure components should be properly namespaced to match the stack names. For example, the project for
managing VPC networking in AWS is located at `src/ol-infrastructure/infrastructure/aws/network/` and the corresponding stacks are defined as
`aws.network.QA` and `aws.network.Production`.


## Executing

In order to run a deployment, you need to specify the project and the stack that you would like to deploy. From the root
of the repository, you can run `pulumi -C src/ol_infrastructure/path/to/module/ up`. If you haven't already selected the
stack, it will ask you to interactively select the stack which you are deploying.


## Adding a new Project

For each deployable unit of work we need to have a Pulumi project defined. The Pulumi CLI has a `new` command, but that
introduces extra files that we don't want. The minimum necessary work to signal that a given directory is a project is
the presence of a `Pulumi.yaml` file and a `__main__.py` where the deployment code is located. The contents of the
`Pulumi.yaml` file should follow this structure:

```
name: ol-infrastructure-dagster-application # Change the name here to be descriptive of the purpose of this project
runtime: python # not necessary to change
description: Pulumi project for deploying the stack of services needed by the Dagster ETL framework # update the description accordingly
backend:
  url: s3://mitol-pulumi-state/ # should not be changed
```

Create the directory path according to the conventions detailed above and then create these files. Once that is done you
will need to create the stack definitions (again according to the above conventions). To do this, `cd` to the target
directory and run the command `pulumi stack init --secrets-provider=awskms://alias/infrastructure-secrets-qa <your.dotted.stack.name.QA>`

Now you're ready to start writing the code that will define the target deployment.


# Configuration Management
In order to streamline the management of infrastructure required to support the services
run by MIT Open Learning we have implemented our configuration management logic using
PyInfra. This has the benefit of being pure Python, allowing us to take advantage of the
broad ecosystem that it provides including linting, testing, and integrations.

## Project Structure
Any given service is unlikely to be composed of a single service, instead requiring a
variety of components to be used together. In order to make the composition of a running
system easier to reason about, we decompose the responsibilities for any given piece of
technology into a self-contained component. These components live under the
`src/bilder/components/` directory. Each component provide at minimum the logic needed to
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

## Directory Locations
When faced with a decision of where to locate any given files or directories we default
to following the [Filesystem Hierarchy
Standard](https://en.wikipedia.org/wiki/Filesystem_Hierarchy_Standard). This typically
means that applications or services which are deployed by downloading an archive or
cloning a software repository should go under `/opt`, configuration files under `/etc/`,
and data files under `/var/lib`.
