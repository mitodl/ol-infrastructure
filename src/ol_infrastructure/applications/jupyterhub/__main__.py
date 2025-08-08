# ruff: noqa: E501, ERA001

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference
from pulumi_aws import get_caller_identity

from bridge.lib.versions import JUPYTERHUB_CHART_VERSION
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
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

jupyterhub_namespace = "jupyter"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(jupyterhub_namespace, ns)
)

# Install the openmetadata helm chart
# https://github.com/mitodl/ol-infrastructure/issues/2680
jupyterhub_application = kubernetes.helm.v3.Release(
    f"jupyterhub-{stack_info.name}-application-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="jupyterhub",
        chart="jupyterhub",
        version=JUPYTERHUB_CHART_VERSION,
        namespace=jupyterhub_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://hub.jupyter.org/helm-chart/",
        ),
        # This is referred to a 'config.yaml' in the documentation
        values={
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
                "config": {
                    "Authenticator": {
                        "admin_users": jupyterhub_config.get_object(
                            "admin_users", default=[]
                        ),
                        "allowed_users": jupyterhub_config.get_object(
                            "allowed_users", default=[]
                        ),
                    },
                    "DummyAuthenticator": {
                        "password": jupyterhub_config.require("shared_password"),
                    },
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
        skip_await=False,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

jupyterhub_shared_plugins = OLApisixSharedPlugins(
    name="ol-jupyterhub-external-service-apisix-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="jupyterhub",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=jupyterhub_namespace,
        k8s_labels=application_labels,
        enable_defaults=True,
    ),
)

jupyterhub_domain = jupyterhub_config.require("domain")
api_tls_secret_name = "api-jupyterhub-tls-pair"  # noqa: S105  # pragma: allowlist secret
cert_manager_certificate = OLCertManagerCert(
    f"ol-jupyterhub-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="jupyterhub",
        k8s_namespace=jupyterhub_namespace,
        k8s_labels=application_labels,
        create_apisixtls_resource=True,
        dest_secret_name=api_tls_secret_name,
        dns_names=[jupyterhub_domain],
    ),
)

jupyterhub_apisix_route = OLApisixRoute(
    name=f"ol-jupyterhub-k8s-apisix-route-{stack_info.env_suffix}",
    k8s_namespace=jupyterhub_namespace,
    k8s_labels=application_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="jupyterhub",
            priority=0,
            shared_plugin_config_name=jupyterhub_shared_plugins.resource_name,
            hosts=[jupyterhub_domain],
            paths=["/*"],
            backend_service_name="proxy-public",
            backend_service_port="http",
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)
