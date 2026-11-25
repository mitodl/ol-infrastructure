"""Marimo Data application stack — published notebook applications.

This stack provisions the shared APISIX/Vault/cert infrastructure for
published (run-mode) MarimoNotebook CRDs at the domain configured via
``marimo_data:apps_domain`` (e.g. ``apps.nb.data.ol.mit.edu`` for Production,
``apps.nb-qa.data.ol.mit.edu`` for QA).  The apps host is distinct from the
JupyterHub host (``jupyterhub_data:domain``) because both stacks create a
Certificate and ApisixTls for their host.  Legacy ApisixRoute hosts are not an
external-dns source, so the apps host is also listed in ``eks:apisix_domains``
for the data cluster.

The marimo-operator is installed on the data EKS cluster via the
``ol-substructure-eks`` stack (``substructure/aws/eks/marimo_operator.py``).
The operator creates a per-``MarimoNotebook`` ClusterIP Service named after
each notebook — there is no central gateway.  Individual notebooks need
their own ``ApisixRoute`` resources that reference the shared OIDC config
(``marimo_oidc``) and shared plugin config (``marimo_shared_plugins``) from
this stack.  The notebook publisher this stack deploys creates them when a hub
user publishes.

Published apps query the warehouse as the viewer; there is no shared warehouse
credential.  The openid-connect plugin logs the viewer in through
``ol-marimo-app-client`` and forwards their access token upstream in
``X-Access-Token`` (the plugin's default), refreshing it from the session's
refresh token once it is within ``oidc_access_token_expires_leeway`` of expiry.
marimo gives notebook code the headers of the request behind each kernel command
through ``mo.app_meta().request``: the WebSocket upgrade when the page loads, then
the POST behind each UI interaction.  Notebook code logs in to StarRocks with
that token (``authentication_openid_connect_client`` to the
``fe_mysql_internal_host`` output of ``applications/starrocks``, port 9030), and
StarRocks forwards it to Gravitino for the governed lake catalog.  The client
plugin refuses to send a token without TLS, which the CI FE doesn't have, and
the FE certificate is for the ``fe_tls_server_name`` output, not the Service
name.

So a per-notebook route has to leave ``X-Access-Token`` in place while it strips
the other identity headers and ``Cookie``.  Notebook code should read the token
in the cell that queries and connect there.  A connection opened at page load
keeps that token after it expires, which StarRocks only checks at login but
Gravitino checks on every catalog call.  A module-level cache is shared by every
viewer of the app, because marimo runs each session as a thread in one process.

Author code in an app can read each viewer's access token, and with it use all
of that viewer's StarRocks and Gravitino grants, writes included, until it
expires (``ol-marimo-app-client`` issues 1h tokens).  A StarRocks connection
it opens before then keeps the viewer's Glue-catalog grants for as long as the
connection stays open, since StarRocks checks the token only at login.  That
is the cost of querying as the viewer.  Published pods run as the
``marimo-published`` ServiceAccount below, which has no IRSA role and no token.

The JupyterHub development environment is managed by the separate
``applications.jupyterhub_data`` stack.
"""

import json

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions

from bridge.lib.versions import MARIMO_RUNTIME_VERSION, NOTEBOOK_PUBLISHER_VERSION
from ol_infrastructure.components.services.apisix import (
    FORWARDED_ACCESS_TOKEN_EXPIRES_LEEWAY_SECONDS,
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixOIDCVaultCredentials,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)

marimo_data_config = Config("marimo_data")
vault_config = Config("vault")

cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")

setup_k8s_provider(kubeconfig=require_stack_output_value(cluster_stack, "kube_config"))

aws_config = AWSBase(
    tags={"OU": BusinessUnit.data, "Environment": stack_info.env_suffix}
)

marimo_namespace = "marimo"

k8s_global_labels = K8sGlobalLabels(
    service=Services.notebooks,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()

application_labels = k8s_global_labels | {
    "ol.mit.edu/application": "marimo-data",
}

apps_domain = marimo_data_config.require("apps_domain")

# Vault policy for the marimo namespace.  sso/marimo-app holds the
# ol-marimo-app-client credentials that ol-apisix-marimo-data-oidc-secrets syncs
# for the gated routes.
marimo_vault_policy_hcl = """
path "secret-operations/data/sso/marimo-app" {
  capabilities = ["read"]
}
path "secret-operations/sso/marimo-app" {
  capabilities = ["read"]
}
"""

marimo_vault_policy = vault.Policy(
    f"ol-marimo-data-vault-policy-{stack_info.env_suffix}",
    name="marimo-data",
    policy=marimo_vault_policy_hcl,
)

marimo_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"ol-marimo-data-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
    role_name="marimo-data",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    # Only the VSO ServiceAccount OLVaultK8SResources creates. Published
    # notebooks run author code in this namespace, and with "*" any pod that
    # mounts a ServiceAccount token could log in and read the OIDC client
    # secret.
    bound_service_account_names=["marimo-data-vault"],
    bound_service_account_namespaces=[marimo_namespace],
    token_policies=[marimo_vault_policy.name],
)

marimo_vault_k8s_resources = OLVaultK8SResources(
    resource_config=OLVaultK8SResourcesConfig(
        application_name="marimo-data",
        namespace=marimo_namespace,
        labels=k8s_global_labels,
        vault_address=vault_config.require("address"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_role_name=marimo_vault_k8s_auth_backend_role.role_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[marimo_vault_k8s_auth_backend_role],
    ),
)

# The identity published notebook pods run as.  It has no IRSA annotation, and
# no token is mounted, so author code gets no AWS or Kubernetes API access
# from it.  Never reuse the hub's IRSA ServiceAccount here.
marimo_published_service_account = kubernetes.core.v1.ServiceAccount(
    f"marimo-published-service-account-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="marimo-published",
        namespace=marimo_namespace,
        labels=application_labels,
    ),
    automount_service_account_token=False,
)

# APISIX OIDC resources for the published-apps gateway (apps_domain).
# Published apps require authentication; all authenticated ol-data-platform
# realm users can view any published app (per v1 scope decision).  What each
# viewer can see in one is their own StarRocks and Gravitino grants.
marimo_oidc = OLApisixOIDCResources(
    f"ol-k8s-apisix-marimo-data-olapisixoidcresources-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="marimo-data",
        k8s_labels=application_labels,
        k8s_namespace=marimo_namespace,
        # Apps log in to StarRocks with the forwarded token.
        oidc_access_token_expires_leeway=FORWARDED_ACCESS_TOKEN_EXPIRES_LEEWAY_SECONDS,
        oidc_logout_path="/logout/oidc",
        oidc_scope="openid profile email",
        oidc_session_idling_timeout=0,
        oidc_session_rolling_timeout=0,
        # Author code can read every request header (mo.app_meta().request),
        # so a published route forwards the viewer's access token, for
        # querying StarRocks as the viewer, and nothing else of theirs.  This
        # is D9 of docs/plans/marimo-first-data-notebooks-spec.md as revised
        # for viewer credentials.
        oidc_set_access_token_header=True,
        oidc_set_userinfo_header=False,
        credentials=OLApisixOIDCVaultCredentials(
            mount="secret-operations",
            path="sso/marimo-app",
            vaultauth=marimo_vault_k8s_resources.auth_name,
        ),
    ),
)

# TLS certificate for the published-apps domain
apps_tls_secret_name = "marimo-apps-tls-pair"  # noqa: S105  # pragma: allowlist secret
OLCertManagerCert(
    f"ol-marimo-data-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="marimo-data",
        k8s_namespace=marimo_namespace,
        k8s_labels=application_labels,
        create_apisixtls_resource=True,
        dest_secret_name=apps_tls_secret_name,
        dns_names=[apps_domain],
    ),
)

# Shared APISIX plugin configuration for the published-apps domain.
# Individual MarimoNotebook deployments reference this via their own
# ApisixRoute resources — the marimo-operator creates a per-notebook
# ClusterIP Service named after each MarimoNotebook; there is no single
# central gateway that can back a static route.
marimo_shared_plugins = OLApisixSharedPlugins(
    name="ol-marimo-data-external-service-apisix-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="marimo-data",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=marimo_namespace,
        k8s_labels=application_labels,
        # Each published notebook is served to the browser from its own route on
        # this same host, so every request its page makes is same-origin.
        # An empty list attaches no cors plugin at all.
        cors_allow_origins=[],
        enable_defaults=True,
        # The default cors entry is allow_origins "**" with credentials, which
        # would let any origin read a gated app's responses with the viewer's
        # APISIX session.  Published apps have no cross-origin caller.
        enable_cors=False,
    ),
)

############################################################
# Notebook publisher
############################################################
# Per-notebook objects (MarimoNotebook, its token Secrets and its ApisixRoute)
# are not managed here.  They are user content: the publisher service
# (ol-data-platform services/notebook-publisher) creates them when a hub user
# publishes, and they are its only state.  This stack owns the platform around
# it: the namespace's guardrails, the publisher's identity and permissions,
# and its Deployment.

# Pod Security Admission. Nothing in the namespace needs more than restricted,
# and the operator's pods meet it through the podOverrides the publisher
# renders.  The EKS stack owns the Namespace, so this only adds labels.
kubernetes.core.v1.NamespacePatch(
    f"marimo-namespace-pod-security-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaPatchArgs(
        name=marimo_namespace,
        labels={
            f"pod-security.kubernetes.io/{mode}": "restricted"
            for mode in ("enforce", "audit", "warn")
        },
    ),
)

# Each publisher user may have NOTEBOOK_PUBLISHER_MAX_NOTEBOOKS_PER_USER apps
# (10 by default) at 100m CPU / 512Mi-1Gi memory / 1Gi-8Gi ephemeral storage.
# The quota bounds the namespace as a whole so published apps can't starve the
# data cluster.
kubernetes.core.v1.ResourceQuota(
    f"marimo-resource-quota-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="marimo-published-apps",
        namespace=marimo_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.core.v1.ResourceQuotaSpecArgs(
        hard={
            "pods": "60",
            "requests.cpu": "12",
            "requests.memory": "40Gi",
            "limits.memory": "72Gi",
            "requests.ephemeral-storage": "80Gi",
            "limits.ephemeral-storage": "500Gi",
        },
    ),
)

# The quota above requires every container to declare these, and the
# operator's init containers don't.
kubernetes.core.v1.LimitRange(
    f"marimo-limit-range-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="marimo-defaults",
        namespace=marimo_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.core.v1.LimitRangeSpecArgs(
        limits=[
            kubernetes.core.v1.LimitRangeItemArgs(
                type="Container",
                default_request={
                    "cpu": "50m",
                    "memory": "128Mi",
                    "ephemeral-storage": "256Mi",
                },
                default={"memory": "512Mi", "ephemeral-storage": "2Gi"},
            )
        ]
    ),
)

notebook_publisher_name = "notebook-publisher"
notebook_publisher_port = 8000
notebook_publisher_labels = application_labels | {
    "app.kubernetes.io/name": notebook_publisher_name,
    # The JupyterHub chart's NetworkPolicy admits hub API clients by this label.
    "hub.jupyter.org/network-access-hub": "true",
}
notebook_publisher_selector = {"app.kubernetes.io/name": notebook_publisher_name}
# The jupyterhub_data stack deploys the hub chart here; its API Service is `hub`.
jupyterhub_namespace = "jupyter-data"

notebook_publisher_service_account = kubernetes.core.v1.ServiceAccount(
    f"notebook-publisher-service-account-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=notebook_publisher_name,
        namespace=marimo_namespace,
        labels=application_labels,
    ),
)

# Never get, list or watch on Secrets: the publisher writes each app's token
# Secrets once and APISIX reads them through the route's secretRef.
notebook_publisher_role = kubernetes.rbac.v1.Role(
    f"notebook-publisher-role-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=notebook_publisher_name,
        namespace=marimo_namespace,
        labels=application_labels,
    ),
    rules=[
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=["marimo.io"],
            resources=["marimos"],
            verbs=["get", "list", "create", "update", "delete"],
        ),
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=["apisix.apache.org"],
            resources=["apisixroutes"],
            verbs=["get", "create", "update"],
        ),
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=[""],
            resources=["secrets"],
            verbs=["create", "delete"],
        ),
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=[""],
            resources=["pods", "pods/log"],
            verbs=["get"],
        ),
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=[""],
            resources=["events"],
            verbs=["list"],
        ),
    ],
)

kubernetes.rbac.v1.RoleBinding(
    f"notebook-publisher-role-binding-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=notebook_publisher_name,
        namespace=marimo_namespace,
        labels=application_labels,
    ),
    role_ref=kubernetes.rbac.v1.RoleRefArgs(
        api_group="rbac.authorization.k8s.io",
        kind="Role",
        name=notebook_publisher_role.metadata.name,
    ),
    subjects=[
        kubernetes.rbac.v1.SubjectArgs(
            kind="ServiceAccount",
            name=notebook_publisher_service_account.metadata.name,
            namespace=marimo_namespace,
        )
    ],
)

# The openid-connect plugin entry every published route starts from.  The
# publisher adds the per-route logout path and the header flags.
notebook_publisher_oidc_file = "oidc-plugin.json"
notebook_publisher_config_map = kubernetes.core.v1.ConfigMap(
    f"notebook-publisher-config-map-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"{notebook_publisher_name}-config",
        namespace=marimo_namespace,
        labels=application_labels,
    ),
    data={
        notebook_publisher_oidc_file: json.dumps(
            marimo_oidc.get_full_oidc_plugin_config(unauth_action="auth")
        )
    },
)

notebook_publisher_container_security_context = kubernetes.core.v1.SecurityContextArgs(
    allow_privilege_escalation=False,
    capabilities=kubernetes.core.v1.CapabilitiesArgs(drop=["ALL"]),
    read_only_root_filesystem=True,
)

kubernetes.apps.v1.Deployment(
    f"notebook-publisher-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=notebook_publisher_name,
        namespace=marimo_namespace,
        labels=notebook_publisher_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=notebook_publisher_selector
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=notebook_publisher_labels,
                annotations={
                    # Roll the pod when the OIDC config changes.
                    "ol.mit.edu/config-version": notebook_publisher_config_map.metadata.resource_version,  # noqa: E501
                },
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name=notebook_publisher_service_account.metadata.name,
                security_context=kubernetes.core.v1.PodSecurityContextArgs(
                    run_as_non_root=True,
                    run_as_user=1000,
                    run_as_group=1000,
                    seccomp_profile=kubernetes.core.v1.SeccompProfileArgs(
                        type="RuntimeDefault"
                    ),
                ),
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name=notebook_publisher_name,
                        image=f"ghcr.io/mitodl/notebook-publisher:{NOTEBOOK_PUBLISHER_VERSION}",
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                name="http", container_port=notebook_publisher_port
                            )
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(name=name, value=value)
                            for name, value in {
                                "JUPYTERHUB_SERVICE_NAME": notebook_publisher_name,
                                "JUPYTERHUB_API_URL": (
                                    f"http://hub.{jupyterhub_namespace}"
                                    ".svc.cluster.local:8081/hub/api"
                                ),
                                "NOTEBOOK_PUBLISHER_NAMESPACE": marimo_namespace,
                                "NOTEBOOK_PUBLISHER_APPS_HOST": apps_domain,
                                "NOTEBOOK_PUBLISHER_RUNTIME_IMAGE": (
                                    "ghcr.io/mitodl/marimo-runtime:"
                                    f"{MARIMO_RUNTIME_VERSION}"
                                ),
                                "NOTEBOOK_PUBLISHER_SERVICE_ACCOUNT": (
                                    "marimo-published"
                                ),
                                "NOTEBOOK_PUBLISHER_OIDC_PLUGIN_FILE": (
                                    f"/etc/notebook-publisher/{notebook_publisher_oidc_file}"
                                ),
                            }.items()
                        ]
                        + [
                            kubernetes.core.v1.EnvVarArgs(
                                name="NOTEBOOK_PUBLISHER_SHARED_PLUGIN_CONFIG_NAME",
                                value=marimo_shared_plugins.resource_name,
                            )
                        ],
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/healthz", port=notebook_publisher_port
                            ),
                        ),
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/healthz", port=notebook_publisher_port
                            ),
                            period_seconds=30,
                        ),
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "100m", "memory": "256Mi"},
                            limits={"memory": "512Mi"},
                        ),
                        security_context=notebook_publisher_container_security_context,
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="config", mount_path="/etc/notebook-publisher"
                            ),
                            # `marimo check` and uvicorn need somewhere to write.
                            kubernetes.core.v1.VolumeMountArgs(
                                name="tmp",
                                mount_path="/tmp",  # noqa: S108
                            ),
                        ],
                    )
                ],
                volumes=[
                    kubernetes.core.v1.VolumeArgs(
                        name="config",
                        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                            name=notebook_publisher_config_map.metadata.name
                        ),
                    ),
                    kubernetes.core.v1.VolumeArgs(
                        name="tmp",
                        empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
                    ),
                ],
            ),
        ),
    ),
)

# JupyterHub proxies /services/notebook-publisher/ here (jupyterhub_data's
# hub.services entry).
kubernetes.core.v1.Service(
    f"notebook-publisher-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=notebook_publisher_name,
        namespace=marimo_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        selector=notebook_publisher_selector,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="http", port=notebook_publisher_port, target_port="http"
            )
        ],
    ),
)

# Admission checks on every MarimoNotebook, so that write access to them (the
# publisher's, if it were compromised) can't become access to anything else in
# the namespace.  Each rule mirrors what the publisher renders.
_overrides = "object.spec.podOverrides"
_containers = f"{_overrides}.containers + ({_overrides}.?initContainers.orValue([]))"
marimo_notebook_policy = kubernetes.admissionregistration.v1.ValidatingAdmissionPolicy(
    f"marimo-notebook-admission-policy-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="marimo-published-notebooks",
        labels=application_labels,
    ),
    spec=kubernetes.admissionregistration.v1.ValidatingAdmissionPolicySpecArgs(
        failure_policy="Fail",
        match_constraints=kubernetes.admissionregistration.v1.MatchResourcesArgs(
            resource_rules=[
                kubernetes.admissionregistration.v1.NamedRuleWithOperationsArgs(
                    api_groups=["marimo.io"],
                    api_versions=["*"],
                    operations=["CREATE", "UPDATE"],
                    resources=["marimos"],
                )
            ]
        ),
        validations=[
            kubernetes.admissionregistration.v1.ValidationArgs(
                expression=expression, message=message
            )
            for expression, message in [
                (
                    f"object.metadata.name != '{notebook_publisher_name}'",
                    "The publisher's own Service has this name.",
                ),
                (
                    "!has(object.spec.sidecars) && !has(object.spec.mounts)"
                    " && !has(object.spec.storage) && !has(object.spec.source)",
                    "Published notebooks have no sidecars, mounts, storage or "
                    "git source.",
                ),
                (
                    "object.spec.auth.password.secretKeyRef.name.startsWith("
                    "object.metadata.name + '-token-')",
                    "The marimo token must come from the app's own token Secret.",
                ),
                (
                    "!has(object.spec.env) || object.spec.env.all(e, "
                    "!has(e.valueFrom))",
                    "Notebook env can't reference Secrets or other objects.",
                ),
                (
                    f"{_overrides}.serviceAccountName == 'marimo-published'"
                    f" && {_overrides}.automountServiceAccountToken == false",
                    "Published notebooks run as marimo-published with no API token.",
                ),
                (
                    f"!has({_overrides}.volumes)",
                    "podOverrides can't add volumes.",
                ),
                (
                    f"{_overrides}.containers.all(c, c.name == 'marimo')"
                    f" && {_overrides}.?initContainers.orValue([]).all(c, "
                    "c.name in ['copy-content', 'setup-venv'])",
                    "podOverrides can only adjust the operator's own containers.",
                ),
                (
                    f"({_containers}).all(c, !has(c.image) && !has(c.envFrom)"
                    " && !has(c.volumeMounts) && !has(c.command)"
                    " && (!has(c.env) || c.env.all(e, !has(e.valueFrom))))",
                    "podOverrides containers can't change image, command, env "
                    "sources or mounts.",
                ),
            ]
        ],
    ),
)

kubernetes.admissionregistration.v1.ValidatingAdmissionPolicyBinding(
    f"marimo-notebook-admission-policy-binding-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="marimo-published-notebooks",
        labels=application_labels,
    ),
    spec=kubernetes.admissionregistration.v1.ValidatingAdmissionPolicyBindingSpecArgs(
        policy_name=marimo_notebook_policy.metadata.name,
        validation_actions=["Deny"],
        match_resources=kubernetes.admissionregistration.v1.MatchResourcesArgs(
            namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={"kubernetes.io/metadata.name": marimo_namespace}
            )
        ),
    ),
)
