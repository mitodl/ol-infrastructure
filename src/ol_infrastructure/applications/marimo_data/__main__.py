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
# data cluster.  The publisher itself runs in the hub's namespace, so a full
# quota blocks new publishes but never the publisher's own rollout.
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
            "limits.ephemeral-storage": "200Gi",
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
    # The JupyterHub chart's NetworkPolicy admits hub API clients in its own
    # namespace by this label.
    "hub.jupyter.org/network-access-hub": "true",
}
notebook_publisher_selector = {"app.kubernetes.io/name": notebook_publisher_name}
# The jupyterhub_data stack deploys the hub chart here; its API Service is `hub`.
# The publisher runs here too, next to the hub and outside the namespace whose
# quota and admission policies constrain the apps it publishes; a Role in
# marimo gives it access there.
jupyterhub_namespace = "jupyter-data"
notebook_publisher_username = (
    f"system:serviceaccount:{jupyterhub_namespace}:{notebook_publisher_name}"
)

notebook_publisher_service_account = kubernetes.core.v1.ServiceAccount(
    f"notebook-publisher-service-account-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=notebook_publisher_name,
        namespace=jupyterhub_namespace,
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
            # delete only matters if OwnerReferencesPermissionEnforcement is
            # on: it then requires it to set ownerReferences.
            verbs=["get", "create", "update", "delete"],
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
            namespace=jupyterhub_namespace,
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
        namespace=jupyterhub_namespace,
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
        namespace=jupyterhub_namespace,
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
        namespace=jupyterhub_namespace,
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

# Admission policies, so that write access to MarimoNotebooks, Secrets and
# ApisixRoutes in marimo (the publisher's, if it were compromised) can't become
# access to anything else.  Each one admits only the shape the publisher
# renders (ol-data-platform services/notebook-publisher, render.py).
notebook_publisher_marimo_port = 2718
notebook_publisher_runtime_image = (
    f"ghcr.io/mitodl/marimo-runtime:{MARIMO_RUNTIME_VERSION}"
)
# Keys the publisher's openid-connect entries may carry: the shared config plus
# the per-route ones it sets.  Anything else (e.g. an introspection endpoint)
# could send the client secret the secretRef merges in somewhere else.
published_route_oidc_keys = sorted(
    {
        *marimo_oidc.get_full_oidc_plugin_config(unauth_action="auth")["config"],
        "unauth_action",
        "logout_path",
        "post_logout_redirect_uri",
        "set_access_token_header",
        "set_id_token_header",
        "set_userinfo_header",
        "set_raw_id_token_header",
        "set_refresh_token_header",
    }
)


def _admission_policy(  # noqa: PLR0913
    name: str,
    resource_group: str,
    resource: str,
    validations: list[tuple[str, str]],
    *,
    variables: dict[str, str] | None = None,
    requested_by: str | None = None,
) -> None:
    """Deny CREATE and UPDATE of a resource in marimo unless every rule holds.

    :param requested_by: Only check requests from this user.
    """
    policy = kubernetes.admissionregistration.v1.ValidatingAdmissionPolicy(
        f"{name}-admission-policy-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=name, labels=application_labels
        ),
        spec=kubernetes.admissionregistration.v1.ValidatingAdmissionPolicySpecArgs(
            failure_policy="Fail",
            match_constraints=kubernetes.admissionregistration.v1.MatchResourcesArgs(
                resource_rules=[
                    kubernetes.admissionregistration.v1.NamedRuleWithOperationsArgs(
                        api_groups=[resource_group],
                        api_versions=["*"],
                        operations=["CREATE", "UPDATE"],
                        resources=[resource],
                    )
                ]
            ),
            match_conditions=[
                kubernetes.admissionregistration.v1.MatchConditionArgs(
                    name="requested-by",
                    expression=f"request.userInfo.username == '{requested_by}'",
                )
            ]
            if requested_by
            else None,
            variables=[
                kubernetes.admissionregistration.v1.VariableArgs(
                    name=variable, expression=expression
                )
                for variable, expression in (variables or {}).items()
            ],
            validations=[
                kubernetes.admissionregistration.v1.ValidationArgs(
                    expression=expression, message=message
                )
                for expression, message in validations
            ],
        ),
    )
    kubernetes.admissionregistration.v1.ValidatingAdmissionPolicyBinding(
        f"{name}-admission-policy-binding-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=name, labels=application_labels
        ),
        spec=kubernetes.admissionregistration.v1.ValidatingAdmissionPolicyBindingSpecArgs(
            policy_name=policy.metadata.name,
            validation_actions=["Deny"],
            match_resources=kubernetes.admissionregistration.v1.MatchResourcesArgs(
                namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                    match_labels={"kubernetes.io/metadata.name": marimo_namespace}
                )
            ),
        ),
    )


_marimo_args = ", ".join(
    f"'{arg}'"
    for arg in [
        "run",
        "--headless",
        "--host=0.0.0.0",
        f"--port={notebook_publisher_marimo_port}",
        "--token-password-file",
        "/etc/marimo/password",
    ]
)
_admission_policy(
    "marimo-published-notebooks",
    "marimo.io",
    "marimos",
    variables={"overrides": "object.spec.podOverrides"},
    validations=[
        (
            "!has(object.spec.sidecars) && !has(object.spec.mounts)"
            " && !has(object.spec.storage) && !has(object.spec.source)"
            " && !has(object.spec.env)",
            "Published notebooks have no sidecars, mounts, storage, git source or env.",
        ),
        (
            f"object.spec.mode == 'run' && object.spec.image == "
            f"'{notebook_publisher_runtime_image}'"
            f" && object.spec.port == {notebook_publisher_marimo_port}",
            "Published notebooks run in the runtime image, in run mode.",
        ),
        (
            "object.spec.auth.password.secretKeyRef.key == 'password'"
            " && object.spec.auth.password.secretKeyRef.name.startsWith("
            "object.metadata.name + '-token-')",
            "The marimo token must come from the app's own token Secret.",
        ),
        (
            "variables.overrides.all(k, k in ['serviceAccountName',"
            " 'automountServiceAccountToken', 'securityContext', 'initContainers',"
            " 'containers'])",
            "podOverrides can only set the ServiceAccount, security contexts and "
            "marimo's args.",
        ),
        (
            "variables.overrides.serviceAccountName == 'marimo-published'"
            " && variables.overrides.automountServiceAccountToken == false",
            "Published notebooks run as marimo-published with no API token.",
        ),
        (
            "variables.overrides.containers.all(c, c.name == 'marimo'"
            " && c.all(k, k in ['name', 'args', 'securityContext'])"
            f" && c.args == [{_marimo_args},"
            " '--base-url=/' + object.metadata.name,"
            " '--sandbox', '/home/marimo/notebooks/notebook.py'])",
            "podOverrides can only give marimo its base URL.",
        ),
        (
            "variables.overrides.?initContainers.orValue([]).all(c,"
            " c.name in ['copy-content', 'setup-venv']"
            " && c.all(k, k in ['name', 'securityContext']))",
            "podOverrides can only set the init containers' security contexts.",
        ),
    ],
)

_admission_policy(
    "marimo-published-notebook-secrets",
    "",
    "secrets",
    requested_by=notebook_publisher_username,
    variables={
        "is_token": "object.metadata.name.matches('-token-[0-9a-f]{8}$')",
        "keys": (
            "object.?data.orValue({}).map(k, k)"
            " + object.?stringData.orValue({}).map(k, k)"
        ),
    },
    validations=[
        (
            "object.type == 'Opaque' && object.metadata.name.matches("
            "'^[a-z]([-a-z0-9]*[a-z0-9])?-(token|route-auth)-[0-9a-f]{8}$')",
            "The publisher only writes Opaque token and route-auth Secrets.",
        ),
        (
            "variables.keys.all(k, variables.is_token ? k == 'password'"
            " : k == 'headers.set.Authorization')",
            "A token Secret holds only the password; a route-auth Secret only the "
            "Authorization header.",
        ),
    ],
)

_admission_policy(
    "marimo-published-routes",
    "apisix.apache.org",
    "apisixroutes",
    variables={"http": "object.spec.http[0]"},
    validations=[
        (
            "!has(object.spec.stream) && size(object.spec.http) == 1"
            " && object.spec.ingressClassName == 'apache-apisix'",
            "A published route has one HTTP rule.",
        ),
        (
            f"variables.http.match.hosts == ['{apps_domain}']"
            " && variables.http.match.paths == ['/' + object.metadata.name,"
            " '/' + object.metadata.name + '/*']"
            " && !has(variables.http.match.exprs)"
            " && !has(variables.http.match.filter_func)",
            "A published route serves only its own path on the apps host.",
        ),
        (
            "size(variables.http.backends) == 1"
            " && variables.http.backends[0].serviceName == object.metadata.name"
            " && variables.http.backends[0].servicePort == "
            f"{notebook_publisher_marimo_port}"
            " && !has(variables.http.upstreams)"
            " && !has(variables.http.authentication)",
            "A published route sends traffic only to its own app.",
        ),
        (
            "variables.http.plugin_config_name == "
            f"'{marimo_shared_plugins.resource_name}'"
            " && !has(variables.http.plugin_config_namespace)"
            " && size(variables.http.plugins) == 2"
            " && variables.http.plugins.all(p, p.name in"
            " ['openid-connect', 'proxy-rewrite'])",
            "A published route uses the shared plugins, OIDC and the token rewrite.",
        ),
        (
            "variables.http.plugins.all(p, p.name != 'openid-connect'"
            f" || (p.secretRef == '{marimo_oidc.secret_name}'"
            f" && p.config.all(k, k in {json.dumps(published_route_oidc_keys)})))",
            "A published route's OIDC config can only change the per-route settings.",
        ),
        (
            "variables.http.plugins.all(p, p.name != 'proxy-rewrite'"
            " || (p.secretRef.startsWith(object.metadata.name + '-route-auth-')"
            " && p.config.all(k, k == 'headers')"
            " && p.config.headers.all(k, k == 'remove')))",
            "A published route only strips headers and injects its own token.",
        ),
    ],
)
