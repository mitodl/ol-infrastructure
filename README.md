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
`Stacks` which are often used as a mapping for different environments. Each module underneath `infrastructure/` is its
own `Project`, meaning that it will have a `Pulumi.yaml` definition of that project.

# Conventions

Stack names should be a dot-separated namespaced representation of the project path, suffixed with an environment specifier in the form of QA or Production. The capitalization is important as it will be used directly to interpolate into tag objects. It is easier to start with QA and Production and then call `.lower()` than it is to build a dictionary mapping the lowercase versions to their properly capitalized representation.

The dotted namespace allows for peaceful coexistence of multiple projects within a single state backend, as well as allowing for use of [stack references](https://www.pulumi.com/docs/tutorials/aws/aws-py-stackreference/) between projects.
