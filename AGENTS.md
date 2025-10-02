# Repository Conventions

## Introduction

This document outlines the conventions and structure of this repository. It is intended
to be used as a guide for developers and as a shared context for AI-powered development
tools to ensure consistency and maintainability of the codebase.

The repository manages cloud infrastructure using a combination of Concourse for CI/CD,
Pulumi for infrastructure as code (IaC), and Packer for building machine images (AMIs).

## Core Technologies

*   **CI/CD**: [Concourse CI](https://concourse-ci.org/)
*   **Infrastructure as Code**: [Pulumi](https://www.pulumi.com/) (using Python)
*   **Image Building**: [HashiCorp Packer](https://www.packer.io/) (using HCL and
    PyInfra for provisioning)
*   **Secrets Management**: [HashiCorp Vault](https://www.vaultproject.io/) and
    [SOPS](https://github.com/getsops/sops)
*   **Primary Language**: Python 3
*   **Configuration & Data Validation**: [Pydantic](https://docs.pydantic.dev/)

## Repository Structure

The `src/` directory contains all the source code, organized by function:

*   `src/bilder/`: Contains Packer configurations for building AMIs.
    *   `components/`: Reusable Packer provisioners and configurations (e.g., installing
        Vault, Consul, Vector).
    *   `images/`: Definitions for specific AMIs, which compose components from
        `src/bilder/components/`.
*   `src/ol_concourse/`: Contains Concourse pipeline definitions, written in Python.
    *   `lib/`: A custom library for generating Concourse pipeline YAML from Python
        objects.
    *   `pipelines/`: The definitions for each CI/CD pipeline.
*   `src/ol_infrastructure/`: Contains Pulumi code for defining and managing cloud
    infrastructure.
    *   `applications/`: Pulumi stacks for specific applications (e.g., `xqwatcher`).
    *   `components/`: Reusable Pulumi components (e.g., an auto-scaling group).
    *   `infrastructure/`: Foundational infrastructure stacks (e.g., AWS networking,
        IAM, EKS).
    *   `lib/`: Helper functions and classes for working with Pulumi and AWS.
*   `src/bridge/`: Utility code that bridges other components, often for handling
    secrets or settings.

## Secrets Management

Secrets are managed using [HashiCorp Vault](https://www.vaultproject.io/) as the central
secrets store. Static secrets that need to be stored in the repository (e.g., initial
passwords, API keys) are encrypted using [SOPS](https://github.com/getsops/sops).

The general workflow is as follows:
1. A static secret is added to a YAML file and encrypted with SOPS. These files are
   often located in `src/bridge/secrets/<context>/` directories within the relevant
   Pulumi project, where the `<context>` is generally scoped to the project that the
   secrets are destined for.
2. During a `pulumi up`, the `src/bridge/secrets/sops.py` utility is used to decrypt the
   SOPS file in memory.
3. The Pulumi program then writes the secret to the appropriate mount and path in Vault.

This approach ensures that secrets are stored securely at rest within the git repository
and are only exposed to authorized systems during infrastructure
deployment. Applications and services then authenticate with Vault to retrieve the
secrets they need at runtime.

## Pulumi Conventions (`src/ol_infrastructure/`)

*   **Language**: All Pulumi code is written in Python.
*   **Structure**: Infrastructure is organized into projects and stacks. Each directory
    within `src/ol_infrastructure/infrastructure/` and
    `src/ol_infrastructure/applications/` typically represents a Pulumi project.
*   **Configuration**: Pydantic models, often inheriting from
    `ol_infrastructure.lib.ol_types.AWSBase`, are used to define the configuration for
    infrastructure components. This provides type safety and validation.
*   **Stack Information**: The `ol_infrastructure.lib.pulumi_helper.StackInfo` dataclass
    provides context about the current Pulumi stack (e.g., name, environment). It is
    instantiated via `parse_stack()`.
*   **Naming**: Pulumi resources should be given a descriptive name that includes the
    component and purpose. Tags are automatically applied, including `business_unit` and
    `environment`.
*   **Kubernetes Resources**: Kubernetes resources (e.g., Helm charts, custom resources,
    operators) are managed using Pulumi's Kubernetes provider. Custom
    `ComponentResource` classes are used to encapsulate common patterns and
    abstractions. Examples include `OLApisixRoute` for managing APISix routes and
    `OLVaultK8SSecret` for creating Kubernetes secrets from Vault.
*   **IAM Policies**: IAM policies are linted using Parliament via
    `ol_infrastructure.lib.aws.iam_helper.lint_iam_policy`.

## Packer Conventions (`src/bilder/`)

*   **Templates**: Packer templates are defined using HCL (`.pkr.hcl` files). Variables
    are used extensively to parameterize builds.
*   **Provisioning**: Provisioning is primarily done using Python scripts, which often
    leverage `pyinfra` for executing commands and managing state on the instance being
    built. These scripts are found in `steps.py` files within component directories.
*   **Components**: Builds are composed of reusable components found in
    `src/bilder/components/`. An image definition in `src/bilder/images/` will typically
    include one or more of these components.
*   **Naming**: AMI names are constructed from variables defined in the HCL files,
    typically including `app_name`, `build_environment`, and a timestamp.

## Concourse Conventions (`src/ol_concourse/`)

*   **Pipeline Generation**: Concourse pipeline YAML files are not written by
    hand. Instead, they are generated from Python code located in
    `src/ol_concourse/pipelines/`.
*   **Modeling**: The `src/ol_concourse/lib/models/` directory contains Pydantic models
    that represent Concourse concepts (e.g., `Pipeline`, `Job`, `GetStep`,
    `TaskStep`). Pipelines are constructed by instantiating and composing these models.
*   **Fragments**: The `PipelineFragment` model is used to create reusable pieces of
    pipelines that can be combined. This is useful for standardizing resource types or
    groups of jobs.
*   **Resources**: Helper functions like `ol_concourse.lib.resources.git_repo` and
    `ol_concourse.lib.resources.registry_image` should be used to define resources in a
    consistent manner.
*   **Jobs**: Helper functions like `ol_concourse.lib.jobs.infrastructure.packer_jobs`
    and `ol_concourse.lib.jobs.infrastructure.pulumi_jobs_chain` are used to generate
    standard job patterns for building images and deploying infrastructure.

## General Python Conventions

*   **Type Hinting**: All code should be fully type-hinted.
*   **Pydantic**: Pydantic is the standard for data modeling, configuration management,
    and data validation. Use `BaseModel` for data structures and `BaseSettings` for
    configuration loaded from the environment.
*   **Dependencies**: Project dependencies are managed with `uv`.
*   **Formatting**: Code should be formatted using `ruff format`.
*   **Linting**: Code should pass `ruff` checks.
