"""Helm's crds/ directory is install-only, so Pulumi has to own those CRDs instead.

The two things that make that adoption work are easy to regress silently: the
patchForce annotation (without it server-side apply stops on a conflict with Helm's
field manager) and the include filter (without it APISIX's bundled standard-channel
Gateway API CRDs would overwrite the experimental ones setup_traefik owns).
"""

import io
import tarfile

import pulumi
import pulumi_kubernetes as kubernetes
import pytest
import yaml as pyyaml
from pulumi import ResourceOptions

from ol_infrastructure.lib import k8s_crds


def _crd(name: str) -> str:
    return pyyaml.safe_dump(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": name},
            "spec": {"versions": [{"name": "v1"}]},
        }
    )


def _archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, body in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(body.encode())
            tar.addfile(info, io.BytesIO(body.encode()))
    return buffer.getvalue()


CHART_FILES = {
    "demo/Chart.yaml": "name: demo\nversion: 1.0.0\n",
    # A templated CRD under templates/ upgrades normally and is not ours to adopt.
    "demo/templates/crds/templated.yaml": _crd("templated.example.com"),
    "demo/crds/own-crds.yaml": _crd("widgets.example.com"),
    "demo/crds/foreign-crds.yaml": _crd("gateways.gateway.networking.k8s.io"),
    # Subchart CRDs are only reachable through the packaged archive.
    "demo/charts/sub/crds/sub-crds.yaml": _crd("gadgets.example.com"),
}

INDEX = pyyaml.safe_dump(
    {
        "entries": {
            "demo": [
                {"version": "0.9.0", "urls": ["demo/demo-0.9.0.tgz"]},
                # Relative URL, as traefik's index uses.
                {"version": "1.0.0", "urls": ["demo/demo-1.0.0.tgz"]},
                # Absolute URL on another host, as apisix's index uses.
                {
                    "version": "2.0.0",
                    "urls": ["https://example.invalid/releases/demo-2.0.0.tgz"],
                },
            ]
        }
    }
)


@pytest.fixture
def fetched(monkeypatch):
    """Serve the index and archive offline, recording the URLs requested."""
    requested = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    def _urlopen(url, timeout=None):  # noqa: ARG001
        requested.append(url)
        if url.endswith("index.yaml"):
            return _Response(INDEX.encode())
        return _Response(_archive(CHART_FILES))

    monkeypatch.setattr(k8s_crds.urllib.request, "urlopen", _urlopen)
    return requested


def test_resolves_repository_relative_url(fetched):
    k8s_crds.fetch_helm_chart_crds("https://charts.example.com", "demo", "1.0.0")
    assert fetched == [
        "https://charts.example.com/index.yaml",
        "https://charts.example.com/demo/demo-1.0.0.tgz",
    ]


def test_resolves_absolute_url_on_another_host(fetched):
    k8s_crds.fetch_helm_chart_crds("https://charts.example.com/", "demo", "2.0.0")
    assert fetched[-1] == "https://example.invalid/releases/demo-2.0.0.tgz"


@pytest.mark.usefixtures("fetched")
def test_takes_subchart_crds_but_not_templated_ones():
    crds = k8s_crds.fetch_helm_chart_crds("https://charts.example.com", "demo", "1.0.0")
    assert [crd["metadata"]["name"] for crd in crds] == [
        "gadgets.example.com",
        "gateways.gateway.networking.k8s.io",
        "widgets.example.com",
    ]


@pytest.mark.usefixtures("fetched")
def test_include_filters_out_foreign_crds():
    crds = k8s_crds.fetch_helm_chart_crds(
        "https://charts.example.com", "demo", "1.0.0", include={"own-crds.yaml"}
    )
    assert [crd["metadata"]["name"] for crd in crds] == ["widgets.example.com"]


@pytest.mark.usefixtures("fetched")
def test_every_crd_is_annotated_for_patch_force():
    crds = k8s_crds.fetch_helm_chart_crds("https://charts.example.com", "demo", "1.0.0")
    assert all(
        crd["metadata"]["annotations"]["pulumi.com/patchForce"] == "true"
        for crd in crds
    )


@pytest.mark.usefixtures("fetched")
def test_unknown_chart_version_is_an_error():
    with pytest.raises(ValueError, match="not found in Helm repository index"):
        k8s_crds.fetch_helm_chart_crds("https://charts.example.com", "demo", "3.0.0")


@pytest.mark.usefixtures("fetched")
def test_layout_change_that_matches_nothing_is_an_error():
    """Adopting zero CRDs looks like success and silently keeps Helm's frozen ones."""
    with pytest.raises(ValueError, match="No CRDs found"):
        k8s_crds.fetch_helm_chart_crds(
            "https://charts.example.com", "demo", "1.0.0", include={"renamed.yaml"}
        )


class _Mocks(pulumi.runtime.Mocks):
    """Record every resource registration so the provider's inputs can be asserted."""

    def __init__(self):
        self.resources = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = args
        return f"{args.name}_id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


CRD_OBJ = {
    "apiVersion": "apiextensions.k8s.io/v1",
    "kind": "CustomResourceDefinition",
    "metadata": {"name": "widgets.example.com"},
}


@pulumi.runtime.test
def test_scoped_provider_sets_upsert_existing_objects():
    """Without it, adopting an already-Helm-created CRD fails "already exists"."""
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)
    original = k8s_crds.fetch_helm_chart_crds
    k8s_crds.fetch_helm_chart_crds = lambda *a, **kw: [CRD_OBJ]  # noqa: ARG005
    try:
        config_group = k8s_crds.adopt_helm_chart_crds(
            "demo-crds",
            kubeconfig="fake-kubeconfig",
            repo="https://charts.example.com",
            chart="demo",
            version="1.0.0",
        )
    finally:
        k8s_crds.fetch_helm_chart_crds = original

    def check(_):
        provider = mocks.resources["demo-crds-provider"]
        assert provider.typ == "pulumi:providers:kubernetes"
        assert provider.inputs["upsertExistingObjects"] in (True, "true", "True")

    return config_group.urn.apply(check)


# Resource options reach the engine without being stored on the resource, and
# Pulumi's MockResourceArgs carries none of them, so the merged options are
# asserted at the one seam that exposes them.

SENTINEL_PROVIDER = object()


def _adoption_opts(caller_opts):
    return k8s_crds._adoption_resource_options(caller_opts, SENTINEL_PROVIDER)


def test_adopted_crds_are_retained_on_delete():
    """Deleting a CRD cascades to every custom resource of that kind."""
    assert _adoption_opts(None).retain_on_delete is True


def test_scoped_provider_overrides_any_caller_provider():
    """The upsert-capable provider is the point; a caller's must not win over it."""
    assert (
        _adoption_opts(ResourceOptions(provider=object())).provider is SENTINEL_PROVIDER
    )


@pulumi.runtime.test
def test_caller_parent_and_depends_on_survive_the_merge():
    """Ordering the caller asked for must not be dropped by the options merge."""
    pulumi.runtime.set_mocks(_Mocks(), preview=False)
    parent = kubernetes.core.v1.Namespace("parent-ns")
    dependency = kubernetes.core.v1.Namespace("dependency-ns")

    opts = _adoption_opts(ResourceOptions(parent=parent, depends_on=[dependency]))

    assert opts.parent is parent
    assert opts.depends_on == [dependency]
    return parent.urn
