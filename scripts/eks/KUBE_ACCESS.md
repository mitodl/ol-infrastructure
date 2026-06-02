# Developer EKS Access

See https://pe.ol.mit.edu/how_to/developer_eks_access/ for initial setup and
background on access requirements.

## Preferred workflow

The preferred entrypoint is the cyclopts-based CLI in `scripts/eks/eks.py`.
It manages a single `~/.kube/config` file covering all OL EKS clusters.

Generate a kubeconfig for all clusters:

```bash
uv run python scripts/eks/eks.py setup
```

By default this creates a **readonly** kubeconfig. You can also choose an
access mode at setup time:

```bash
uv run python scripts/eks/eks.py setup --mode readonly
uv run python scripts/eks/eks.py setup --mode developer
uv run python scripts/eks/eks.py setup --mode admin
```

Optional current context:

```bash
uv run python scripts/eks/eks.py setup --mode readonly --current-context applications-qa
```

## How auth works

The generated kubeconfig uses an `exec` plugin that calls back into
`scripts/eks/eks.py`.

- `readonly` and `developer` modes are OIDC-first.
- Vault authentication and generated AWS credentials are cached locally under
  `~/.cache/ol-infrastructure/eks/`.
- Users do **not** need to run `source eks.env` or manually refresh temporary
  AWS credentials before using `kubectl`.
- The tool currently manages and overwrites `~/.kube/config` directly.

## Access modes

- `readonly` — safe exploratory access everywhere using `AmazonEKSViewPolicy`
- `developer` — existing shared developer write permissions
- `admin` — existing cluster-specific admin access

> Note: admin mode still relies on the existing AWS assume-role path for the
> cluster admin role. Readonly and developer modes are the primary OIDC-first,
> cached flows.

## Legacy helper

`scripts/eks/eks.sh` is still present for compatibility, but it represents the
older workflow that required generating shell AWS credentials separately. Prefer
`uv run python scripts/eks/eks.py setup ...` for new usage.
