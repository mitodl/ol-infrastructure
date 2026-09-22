"""Give Pulumi ownership of the CRDs a Helm chart ships in its ``crds/`` directory.

Helm's ``crds/`` directory is install-only: ``helm upgrade`` never touches it. Any
CRD shipped there therefore freezes at whatever chart version first landed on a
cluster, while the controller Deployment is upgraded past it indefinitely. The
failure is silent -- fields the newer schema adds are accepted by Pulumi and by the
API server, then dropped by schema pruning, with no error anywhere.

The fix is to let Pulumi apply the CRDs as their own resource ahead of the release
and set ``skip_crds=True`` so Helm stays out of it on fresh installs too.

Adoption of a CRD that Helm already created needs two things that are easy to miss:

1. A provider scoped to just the CRDs with ``upsert_existing_objects=True``, or the
   first apply fails with ``... already exists``. It is deliberately not set on the
   cluster-wide provider, where it would let any resource silently take over an
   unrelated pre-existing object that Pulumi never created.
2. ``pulumi.com/patchForce`` on each CRD, or server-side apply stops on
   ``conflicts with "helm"`` -- Helm's field manager owns the schema fields.
"""

from __future__ import annotations

import tarfile
import urllib.request
from collections.abc import Collection
from io import BytesIO
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import pulumi_kubernetes as kubernetes
import yaml as pyyaml
from pulumi import ResourceOptions

if TYPE_CHECKING:
    import pulumi

CHART_FETCH_TIMEOUT_SECONDS = 60


def _resolve_chart_url(repo: str, chart: str, version: str) -> str:
    """Look up a chart archive's download URL in its Helm repository index.

    Resolving through ``index.yaml`` rather than constructing the URL is what makes
    this work across repositories: the three charts this is used for disagree on
    layout. traefik nests the archive a directory deep, apisix points at a GitHub
    release asset on another host entirely, and only eks-charts is flat. A
    hand-built ``{repo}/{chart}-{version}.tgz`` 404s on two of the three.

    Returns:
        The absolute URL of the packaged chart.

    Raises:
        ValueError: If the repository index has no entry for that chart version.
    """
    index_url = f"{repo.rstrip('/')}/index.yaml"
    with urllib.request.urlopen(  # noqa: S310
        index_url, timeout=CHART_FETCH_TIMEOUT_SECONDS
    ) as response:
        index = pyyaml.safe_load(response.read())
    for entry in index.get("entries", {}).get(chart, []):
        if entry.get("version") == version:
            # urljoin passes an absolute URL through untouched and resolves a
            # repository-relative one against the index, which covers both shapes.
            return urljoin(index_url, entry["urls"][0])
    msg = f"Chart {chart} {version} not found in Helm repository index {index_url}"
    raise ValueError(msg)


def fetch_helm_chart_crds(
    repo: str,
    chart: str,
    version: str,
    *,
    groups: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Read the CRDs out of a packaged Helm chart, ready to be applied by Pulumi.

    Reads the same archive Helm itself installs, so the CRDs cannot drift from the
    chart version pinned on the release. That also picks up subchart CRDs, which
    live at ``{chart}/charts/{subchart}/crds/`` -- the APISIX ingress controller's
    CRDs are only reachable this way. Taking the packaged archive additionally
    sidesteps the symlink trap in sourcing CRDs by raw GitHub URL: a chart's
    ``crds/`` entry may be a symlink, and raw.githubusercontent.com serves a
    symlink's target *path* as the response body instead of following it.

    Args:
        repo: Helm repository URL, as passed to the release's ``repository_opts``.
        chart: Chart name.
        version: Exact chart version, which should be the same value pinned on the
            release so the two cannot diverge.
        groups: API groups (``spec.group``) to keep, e.g. ``{"apisix.apache.org"}``.
            Omit to take every CRD in the chart. **Pass this whenever a chart
            bundles CRDs owned by something else.** The APISIX ingress controller
            ships the standard-channel Gateway API CRDs alongside its own; applying
            those here would force the cluster-scoped ``gateway.networking.k8s.io``
            CRDs down from the experimental channel that setup_traefik installs and
            owns. Filtering on group rather than archive file name is deliberate:
            a chart that renames, splits, or adds a CRD file keeps matching, so a
            new CRD in the chart's own group is adopted instead of being created
            by nobody (``skip_crds=True`` keeps Helm from creating it either).

    Returns:
        The CRD manifests, ordered by name, each annotated so that server-side
        apply can take the schema over from Helm's field manager.

    Raises:
        ValueError: If the chart ships no matching CRDs, which means the chart
            layout changed and the caller is now silently adopting nothing.
    """
    with urllib.request.urlopen(  # noqa: S310
        _resolve_chart_url(repo, chart, version), timeout=CHART_FETCH_TIMEOUT_SECONDS
    ) as response:
        archive = BytesIO(response.read())

    crds: list[dict[str, Any]] = []
    with tarfile.open(fileobj=archive, mode="r:gz") as tar:
        for member in tar.getmembers():
            path = member.name.split("/")
            # Match crds/ at any depth so subchart CRDs come along, but only a real
            # crds/ directory. A templates/crds/ (KEDA, VPA, typesense-operator) is
            # rendered like any other template and upgrades normally, so adopting it
            # would take ownership of CRDs that were never frozen.
            directories = path[:-1]
            if (
                not member.isfile()
                or "crds" not in directories
                or "templates" in directories
            ):
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            for doc in pyyaml.safe_load_all(extracted.read().decode("utf-8")):
                if not doc or doc.get("kind") != "CustomResourceDefinition":
                    continue
                if groups is not None and doc["spec"]["group"] not in groups:
                    continue
                doc["metadata"].setdefault("annotations", {})[
                    "pulumi.com/patchForce"
                ] = "true"
                crds.append(doc)

    if not crds:
        msg = (
            f"No CRDs found in chart {chart} {version}"
            f"{f' in groups {sorted(groups)}' if groups else ''}"
        )
        raise ValueError(msg)
    return sorted(crds, key=lambda crd: crd["metadata"]["name"])


def _adoption_resource_options(
    opts: ResourceOptions | None, crd_provider: kubernetes.Provider
) -> ResourceOptions:
    """Build the ConfigGroup's options, layering adoption settings over the caller's.

    Split out from adopt_helm_chart_crds because neither Pulumi's mock resource
    args nor the constructed resource retains resource options, so this is the
    only seam at which they can be asserted.

    Returns:
        The caller's options with the scoped provider and CRD retention applied.
    """
    return ResourceOptions.merge(
        opts,
        ResourceOptions(
            provider=crd_provider,
            # Deleting a CRD cascades to every custom resource of that kind, so a
            # destroy, a removed caller, or a groups= change that drops a group
            # must not take the CRD with it -- these objects predate Pulumi and
            # are only adopted here. Propagates from the ConfigGroup to each child
            # CRD, which was confirmed against the deployed starrocks stack.
            retain_on_delete=True,
        ),
    )


def adopt_helm_chart_crds(  # noqa: PLR0913
    resource_name: str,
    *,
    kubeconfig: pulumi.Input[str],
    repo: str,
    chart: str,
    version: str,
    groups: Collection[str] | None = None,
    opts: ResourceOptions | None = None,
) -> kubernetes.yaml.v2.ConfigGroup:
    """Apply a chart's CRDs as a Pulumi resource, adopting what Helm already created.

    Order this before the chart's release with ``depends_on`` and set
    ``skip_crds=True`` on that release.

    Args:
        resource_name: Base name for the CRD resources, conventionally
            ``f"{cluster_name}-{chart}-crds"``.
        kubeconfig: Kubeconfig for the scoped provider, normally
            ``cluster.kubeconfig``.
        repo: Helm repository URL.
        chart: Chart name.
        version: Exact chart version pinned on the release.
        groups: API groups to keep. See fetch_helm_chart_crds.
        opts: Resource options for the ConfigGroup. The scoped provider is supplied
            here and overrides any provider passed in, which is the point of this
            function -- pass ``parent``/``depends_on`` only.

    Returns:
        The ConfigGroup holding the CRDs, to be named in the release's
        ``depends_on``.
    """
    crd_provider = kubernetes.Provider(
        f"{resource_name}-provider",
        kubeconfig=kubeconfig,
        upsert_existing_objects=True,
    )
    return kubernetes.yaml.v2.ConfigGroup(
        resource_name,
        objs=fetch_helm_chart_crds(repo, chart, version, groups=groups),
        opts=_adoption_resource_options(opts, crd_provider),
    )
