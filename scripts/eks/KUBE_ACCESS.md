# Developer EKS Access

See https://engineering.ol.mit.edu/getting_started_and_how_tos/eks_access_setup/ for initial setup and
background on access requirements.

## Preferred workflow

The preferred entrypoint is the cyclopts-based CLI in `scripts/eks/eks.py`.
It writes `~/.kube/ol-eks.yaml`, one kubeconfig covering all OL EKS clusters,
kept apart from `~/.kube/config`, where local-dev registers its cluster.

Generate a kubeconfig for all clusters:

```bash
uv run python scripts/eks/eks.py setup
```

Name the file for each command that should reach EKS, so a bare `kubectl` keeps
targeting local-dev:

```bash
KUBECONFIG=~/.kube/ol-eks.yaml kubectl --context applications-qa get pods -n mitxonline
```

CLIs such as `kubectl`, `k9s` and `helm` also take the file as a flag, which
reads the same in every shell:

```bash
kubectl --kubeconfig ~/.kube/ol-eks.yaml --context applications-qa get pods -n mitxonline
```

Most Kubernetes tools read `KUBECONFIG` when launched from a shell. Quit a GUI
app such as Headlamp before relaunching it this way:

```bash
KUBECONFIG=~/.kube/ol-eks.yaml k9s
KUBECONFIG=~/.kube/ol-eks.yaml open -a Headlamp   # Headlamp on mac
KUBECONFIG=~/.kube/ol-eks.yaml headlamp           # Headlamp on linux
# etc.
```

Avoid Headlamp's "Add Cluster" for these clusters: it stores a copy of the
kubeconfig, and the copy keeps the old access mode after you run `setup` again.

Earlier versions of `setup` wrote EKS entries into `~/.kube/config`. While
those remain, `setup` and `local-dev/scripts/start.sh` both refuse to run.
Remove them, keeping everything else in the file (it is copied to
`~/.kube/config.bak-<timestamp>` first):

```bash
uv run python scripts/eks/eks.py prune-default-kubeconfig
```

`setup --output-path ~/.kube/config` still works: it replaces that whole file,
local-dev's entries included, with no backup, and `start.sh` then refuses to
start.

By default `setup` creates a **developer** (read/write) kubeconfig. You can also
choose an access mode at setup time:

```bash
uv run python scripts/eks/eks.py setup --mode readonly
uv run python scripts/eks/eks.py setup --mode developer
uv run python scripts/eks/eks.py setup --mode admin
```

The kubeconfig sets no current context, so `kubectl` reaches these clusters
only when you name both the file and a context
(`KUBECONFIG=~/.kube/ol-eks.yaml kubectl --context applications-qa ...`). To set
one anyway:

```bash
uv run python scripts/eks/eks.py setup --mode readonly --current-context applications-qa
```

Optional paired readonly contexts for automation tools:

```bash
uv run python scripts/eks/eks.py setup --mode developer --include-readonly-contexts
```

## How auth works

The generated kubeconfig uses an `exec` plugin that calls back into
`scripts/eks/eks.py`.

- `readonly` and `developer` modes are OIDC-first.
- Vault authentication and generated AWS credentials are cached locally under
  `~/.cache/ol-infrastructure/eks/`.
- Users do **not** need to run `source eks.env` or manually refresh temporary
  AWS credentials before using `kubectl`.
- `setup` overwrites its output file (`--output-path`, default
  `~/.kube/ol-eks.yaml`) on every run.
- Developer and admin kubeconfigs write only operator contexts by default.
  Add `--include-readonly-contexts` if you also want paired `-readonly`
  contexts for automation.

## Access modes

- `readonly` — safe exploratory access everywhere using `AmazonEKSViewPolicy`
- `developer` — existing shared developer write permissions
- `admin` — existing cluster-specific admin access

> Note: admin mode still relies on the existing AWS assume-role path for the
> cluster admin role. Readonly and developer modes are the primary OIDC-first,
> cached flows.
