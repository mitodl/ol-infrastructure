# Agent Instructions — `src/bilder`

PyInfra + Packer AMI build system. Packer drives the EC2 instance lifecycle; PyInfra
(`deploy.py`) runs inside the instance to configure it. These two tools are tightly
coupled — understanding both is required before modifying either.

## How AMI Builds Work

```
Packer reads *.pkr.hcl  →  launches EC2 instance from base AMI
                         →  runs deploy.py via PyInfra over SSH
                         →  snapshots the configured instance as a new AMI
```

The `.pkr.hcl` files and `deploy.py` in the same image directory are a matched pair.
Packer passes variables to PyInfra via environment variables read in `deploy.py`.

## Directory Layout

```
src/bilder/
  images/                   # One directory per AMI build target
    consul/
      deploy.py             # PyInfra provisioning script (the "what to install")
      consul.pkr.hcl        # Packer template (the "how to build")
      files/                # Static files copied to the instance
      templates/            # Jinja2 config templates
    vault/
    concourse/
    ...
  components/               # Reusable provisioning components (imported by deploy.py)
    hashicorp/              # Consul, Vault, Nomad installers
    baseline/               # Base OS setup, users, SSH hardening
    vector/                 # Vector log agent
    alloy/                  # Grafana Alloy agent
    caddy/                  # Caddy web server
    docker/                 # Docker daemon setup
    traefik/                # Traefik proxy
    ...
  facts/                    # PyInfra fact classes for host introspection
  lib/                      # Shared Python utilities for build scripts
  deployments/              # Non-AMI deployment scripts (PyInfra only, no Packer)
```

## Writing a `deploy.py`

PyInfra operations must be **idempotent** — safe to run multiple times. Use operations
from `pyinfra.operations`, not raw shell commands.

```python
import os
from pyinfra import host
from pyinfra.operations import files, systemd

from bilder.components.baseline.steps import install_baseline_packages
from bilder.components.hashicorp.consul.models import Consul, ConsulConfig
from bilder.components.hashicorp.steps import (
    configure_hashicorp_product,
    install_hashicorp_products,
)
from bilder.facts.has_systemd import HasSystemd
from bridge.lib.versions import CONSUL_VERSION

# Versions come from environment variables set by Packer (fall back to bridge defaults)
consul_version = os.environ.get("CONSUL_VERSION", CONSUL_VERSION)

# Reuse components instead of reimplementing
install_baseline_packages()
consul = Consul(version=consul_version, configuration={...})
install_hashicorp_products([consul])
configure_hashicorp_product(consul)

# Deploy static config files
files.put(
    src="files/myapp.conf",
    dest="/etc/myapp/myapp.conf",
    mode="0644",
)

# Manage services
if host.get_fact(HasSystemd):
    systemd.service(
        service="myapp",
        enabled=True,
        running=True,
    )
```

## Using Existing Components

Always check `src/bilder/components/` before writing new provisioning logic:

| Component | Import path | What it does |
|-----------|------------|-------------|
| Baseline OS setup | `bilder.components.baseline.steps.install_baseline_packages` | apt packages, system baseline |
| HashiCorp products | `bilder.components.hashicorp.steps.install_hashicorp_products` | Installs Consul, Vault, Nomad, etc. |
| HashiCorp config | `bilder.components.hashicorp.steps.configure_hashicorp_product` | Renders config files from models |
| Vector log agent | `bilder.components.vector` | Ships logs to Grafana/Loki |
| Alloy agent | `bilder.components.alloy` | Grafana Alloy observability agent |
| Docker | `bilder.components.docker` | Docker daemon + compose |

## HCL Files

Packer HCL follows its own formatting standard — always run:

```bash
packer fmt -recursive src/bilder/
```

before committing. The CI pipeline will reject incorrectly formatted HCL.

Validate a template (requires AWS credentials):

```bash
cd src/bilder/images/consul/
packer validate .
```

Shared HCL lives in `src/bilder/images/` (the images root, not the package root):

- `packer.pkr.hcl` — required plugins
- `config.pkr.hcl` — shared source AMI lookup
- `variables.pkr.hcl` — common variables (app_name, node_type, etc.)

## Adding a New Image

1. Create `src/bilder/images/<name>/` with `deploy.py` and `<name>.pkr.hcl`
2. Reference the shared `config.pkr.hcl` and `variables.pkr.hcl` from `src/bilder/images/`
3. Compose from existing components; add a new component only if the logic is reusable
4. Run `packer fmt -recursive src/bilder/` and `packer validate src/bilder/images/<name>/`
5. Add a Concourse pipeline entry in `src/ol_concourse/pipelines/infrastructure/`

## Adding a New Component

Components live in `src/bilder/components/<category>/`. A component is a Python module
that exports functions called from `deploy.py`. Keep components side-effect-free when
possible (configure, don't start services — leave that to the image's `deploy.py`).

## Validation

```bash
packer fmt -recursive src/bilder/        # format HCL
packer validate src/bilder/images/<name>/  # validate template syntax

uv run ruff format src/bilder/
uv run ruff check src/bilder/
uv run mypy src/bilder/
```

## Where NOT to put code

- Pulumi resource declarations → `src/ol_infrastructure/`
- Concourse pipeline definitions → `src/ol_concourse/`
- Constants shared with `ol_infrastructure` → `src/bridge/`
- One-off operational scripts → `scripts/`
