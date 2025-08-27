# ruff: noqa: E501, ERA001, FIX002

from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, ResourceOptions, StackReference
from pulumi_aws import get_caller_identity

from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()

jupyterhub_config = Config("jupyterhub")
binderhub_config = Config("binderhub")

dns_stack = StackReference("infrastructure.aws.dns")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
consul_stack = StackReference(f"infrastructure.consul.operations.{stack_info.name}")
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")

apps_vpc = network_stack.require_output("applications_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]
aws_config = AWSBase(
    tags={"OU": BusinessUnit.mit_learn, "Environment": stack_info.env_suffix}
)

vault_config = Config("vault")
setup_vault_provider(stack_info)

k8s_global_labels = K8sGlobalLabels(
    service=Services.jupyterhub,
    ou=BusinessUnit.mit_learn,
    stack=stack_info,
).model_dump()

application_labels = k8s_global_labels | {
    "ol.mit.edu/application": "jupyterhub",
}

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
aws_account = get_caller_identity()

namespace = "jupyter"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(namespace, ns)
)

# Create vault_k8s_resources to allow jupyter hub to access secrets in vault
jupyterhub_vault_policy = vault.Policy(
    f"ol-jupyterhub-vault-policy-{stack_info.env_suffix}",
    name="jupyterhub",
    policy=Path(__file__).parent.joinpath("jupyterhub_policy.hcl").read_text(),
)

jupyterhub_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"ol-jupyterhub-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
    role_name="jupyterhub",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[namespace],
    token_policies=[jupyterhub_vault_policy.name],
)

vault_k8s_resources = OLVaultK8SResources(
    resource_config=OLVaultK8SResourcesConfig(
        application_name="jupyterhub",
        namespace=namespace,
        labels=k8s_global_labels,
        vault_address=vault_config.require("address"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_role_name=jupyterhub_vault_k8s_auth_backend_role.role_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[jupyterhub_vault_k8s_auth_backend_role],
    ),
)

shared_apisix_plugins = OLApisixSharedPlugins(
    name="ol-jupyterhub-external-service-apisix-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="jupyterhub",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=namespace,
        k8s_labels=application_labels,
        enable_defaults=True,
    ),
)

jupyterhub_domain = jupyterhub_config.require("domain")
binderhub_domain = binderhub_config.require("domain")

api_tls_secret_name = "api-jupyterhub-tls-pair"  # noqa: S105  # pragma: allowlist secret
shared_cert_manager_certificate = OLCertManagerCert(
    f"ol-jupyterhub-binderhub-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="jupyterhub",
        k8s_namespace=namespace,
        k8s_labels=application_labels,
        create_apisixtls_resource=True,
        dest_secret_name=api_tls_secret_name,
        dns_names=[jupyterhub_domain, binderhub_domain],
    ),
)

oidc_resources = OLApisixOIDCResources(
    f"ol-k8s-apisix-olapisixoidcresources-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="jupyterhub",
        k8s_labels=application_labels,
        k8s_namespace=namespace,
        oidc_logout_path="hub/logout",
        # There is no hookied in logout so this doesn't matter ATM
        oidc_post_logout_redirect_uri=f"https://{jupyterhub_domain}/hub/login",
        oidc_session_cookie_lifetime=60 * 20160,
        oidc_use_session_secret=True,
        oidc_scope="openid email",
        vault_mount="secret-operations",
        vault_path="sso/jupyterhub",
        vaultauth=vault_k8s_resources.auth_name,
    ),
)

apisix_route = OLApisixRoute(
    name=f"ol-jupyterhub-k8s-apisix-route-{stack_info.env_suffix}",
    k8s_namespace=namespace,
    k8s_labels=application_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="jupyterhub",
            priority=0,
            shared_plugin_config_name=shared_apisix_plugins.resource_name,
            plugins=[
                oidc_resources.get_full_oidc_plugin_config(unauth_action="auth"),
            ],
            hosts=[jupyterhub_domain],
            paths=["/*"],
            backend_service_name="proxy-public",
            backend_service_port="http",
            websocket=True,
        ),
        OLApisixRouteConfig(
            route_name="binderhub",
            priority=1,
            shared_plugin_config_name=shared_apisix_plugins.resource_name,
            plugins=[
                oidc_resources.get_full_oidc_plugin_config(unauth_action="auth"),
            ],
            hosts=[binderhub_domain],
            paths=["/*"],
            backend_service_name="binder",
            backend_service_port=80,
            websocket=True,
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

# We need to know the dockerhub creds at stack run time.
# Chart provides no provisions for sourcing these from a secret in k8s.
dockerhub_secret = vault.generic.get_secret_output(
    path="secret-global/dockerhub",
    opts=InvokeOptions(parent=jupyterhub_vault_k8s_auth_backend_role),
)

# Binderhub and jupyterhub installation
# Installing the binderhub will come with a subchart installation of jupyterhub
#
# Ref: https://github.com/jupyterhub/binderhub/blob/main/helm-chart/binderhub/values.yaml
# Ref: https://github.com/jupyterhub/zero-to-jupyterhub-k8s/blob/main/jupyterhub/values.yaml
binderhub_application = kubernetes.helm.v3.Release(
    f"binderhub-{stack_info.name}-application-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="binderhub",
        chart="binderhub",
        namespace=namespace,
        cleanup_on_fail=True,
        version="1.0.0-0.dev.git.3782.he87eff2d",  # TODO(Mike): Put this in versions.py
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://hub.jupyter.org/helm-chart/",
        ),
        values={
            "service": {"type": "NodePort", "nodePort": 30001},
            # Use dockerhub as the registry for now.
            "registry": {
                "username": dockerhub_secret.data.apply(
                    lambda data: "{}".format(data["username"])
                ),
                "password": dockerhub_secret.data.apply(
                    lambda data: "{}".format(data["password"])
                ),
            },
            "ingress": {
                "enabled": False,
            },
            "jupyterhub": {
                "proxy": {
                    "service": {
                        "type": "NodePort",
                        "nodePorts": {
                            "http": 30000,
                            "https": 30443,
                        },
                    },
                    "chp": {
                        "resources": {
                            "requests": {
                                "cpu": "100m",
                                "memory": "64Mi",
                            },
                            "limits": {
                                "cpu": "100m",
                                "memory": "64Mi",
                            },
                        },
                    },
                },
                "scheduling": {
                    "userScheduler": {
                        "resources": {
                            "requests": {
                                "cpu": "100m",
                                "memory": "64Mi",
                            },
                            "limits": {
                                "cpu": "100m",
                                "memory": "64Mi",
                            },
                        },
                    },
                },
                "hub": {
                    "extraConfig": {
                        "dynamicImageConfig.py" : Path(__file__).parent.joinpath("dynamicImageConfig.py").read_text()
                    },
                    "config": {
                        "BinderSpawner": {
                            "auth_enabled": True,
                        },
                        "Authenticator": {
                            "admin_users": jupyterhub_config.get_object(
                                "admin_users", default=[]
                            ),
                            "allowed_users": jupyterhub_config.get_object(
                                "allowed_users", default=[]
                            ),
                        },
                        # Uncomment to set the password from config.
                        # "DummyAuthenticator": {
                        #     "password": jupyterhub_config.require("shared_password"),
                        # },
                        "JupyterHub": {
                            "authenticator_class": "dummy",
                        },
                    },
                    "db": {
                        "pvc": {
                            "storage": "10Gi",
                        }
                    },
                    "resources": {
                        "requests": {
                            "cpu": "100m",
                            "memory": "256Mi",
                        },
                        "limits": {
                            "cpu": "100m",
                            "memory": "256Mi",
                        },
                    },
                },
                "prePuller": {
                    "resources": {
                        "requests": {
                            "cpu": "10m",
                            "memory": "10Mi",
                        },
                        "limits": {
                            "cpu": "10m",
                            "memory": "10Mi",
                        },
                    }
                },
                "singleuser": {
                    # This is where we would do our own notebook image
                    # ref: https://z2jh.jupyter.org/en/stable/jupyterhub/customizing/user-environment.html#customize-an-existing-docker-image
                    # "image": {
                    #     "name": "mitodl/some-special-image"
                    #     "tag": "some-tag",
                    # },
                    # Below is similar but not the same as k8s resource declarations.
                    # These are on a PER-USER-BASIS, so they can quickly grow with lots of
                    # users. Numbers are conservative to start with.
                    "image": {
                        "name": "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks",
                        "tag": "clustering_and_descriptive_ai",
                    },
                    "allowPrivilegeEscalation": True,
                    "cmd": [
                        "jupyterhub-singleuser",
                    ],
                    "startTimeout": 600,
                    "networkPolicy": {
                        "enabled": False,
                    },
                    "memory": {
                        "limit": "1G",
                        "guarantee": "256M",
                    },
                    "cpu": {
                        "limit": 1,
                        "guarantee": 0.25,
                    },
                    "extraEnv": {
                        # This is the modern UI experience
                        "JUPYTERHUB_SINGLEUSER_APP": "jupyter_server.serverapp.ServerApp"
                    },
                    "cloudMetadata": {
                        "blockWithIptables": False,  # this should really be true but it isn't working right now
                    },
                },
            },
            "config": {
                "BinderHub": {
                    "hub_url": f"https://{jupyterhub_domain}/",
                    "hub_url_local": "http://proxy-public.jupyter.svc.cluster.local/",
                    "use_registry": True,
                    "image_prefix": "mitodl/binderhub-",
                },
            },
            "imageBuilderType": "dind",
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)
