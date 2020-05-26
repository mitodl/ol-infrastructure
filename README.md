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

