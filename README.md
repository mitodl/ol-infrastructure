# Getting Started
This is a Pulumi project, so the first step is to install the Pulumi CLI along with the relevant language
protocol. Instructions [here](https://www.pulumi.com/docs/get-started/install/).

To install and manage the relevant dependencies this project uses [Poetry](https://python-poetry.org/). After installing
the Poetry CLI simply run `poetry install`.

We use the S3 state backend for Pulumi, so after installing the Pulumi CLI run `pulumi login s3://mitol-pulumi-state`.

# Structure

This is a monorepo of the Pulumi code that we use to manage the infrastructure that powers MIT Open Learning

Component resources are located under `components/` with a descriptive name that makes it evident what the intent of the
component is.

Concrete implementations of infrastructure are located under `infrastructure/` with a descriptive name that makes it
evident what is contained in that project.

Each component or concrete infrastructure that is more complex than a single resource will include a `diagram.py` file
that uses the [diagrams](https://diagrams.mingrammer.com/) package to illustrate the system structure that it creates.

# Nomenclature

Pulumi organizes code into `Projects` which represent a deployable unit. Within a project they have a concept of
`Stacks` which are often used as a mapping for different environments. Each module underneath `infrastructure/` and
`applications/` is its own `Project`, meaning that it will have a `Pulumi.yaml` definition of that project. Each `stack`
has its own yaml file in which the configuration for that stack is defined.

# Conventions

Stack names should be a dot-separated namespaced representation of the project path, suffixed with an environment
specifier in the form of QA or Production. The capitalization is important as it will be used directly to interpolate
into tag objects. It is easier to start with QA and Production and then call `.lower()` than it is to build a dictionary
mapping the lowercase versions to their properly capitalized representation.

The dotted namespace allows for peaceful coexistence of multiple projects within a single state backend, as well as
allowing for use of [stack references](https://www.pulumi.com/docs/tutorials/aws/aws-py-stackreference/) between
projects.

The infrastructure components should be properly namespaced to match the stack names. For example, the project for
managing VPC networking in AWS is located at `infrastructure/aws/network/` and the corresponding stacks are defined as
`aws.network.QA` and `aws.network.Production`.


# Executing

In order to run a deployment, you need to specify the project and the stack that you would like to deploy. From the root
of the repository, you can run `pulumi -C src/ol_infrastructure/path/to/module/ up`. If you haven't already selected the
stack, it will ask you to interactively select the stack which you are deploying.

Some resources will accept environment variables as configuration parameters. This reduces the boilerplate for per-stack
configurations. Resources that we rely on which will accept environment variables are:

Vault:
  - `VAULT_ADDR`: The full URL (excluding https://) where the Vault server is located
  - `VAULT_TOKEN`: The token to be used for authenticating to Vault

SaltStack:
  - `SALTSTACK_API_URL`: The full URL for communicating with the Salt API (e.g. https://salt-qa.odl.mit.edu)
  - `SALTSTACK_API_USER`: The username for authenticating to the Salt API (default is `pulumi`)
  - `SALTSTACK_API_PASSWORD`: The password for the Salt API user


# Adding a new Project

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
directory and run the command `pulumi stack init --secrets-provider=awskms://alias/PulumiSecrets <your.dotted.stack.name.QA>`

Now you're ready to start writing the code that will define the target deployment.
