# ruff: noqa: E501, ERA001, FIX002

from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, ResourceOptions, StackReference
from pulumi_aws import ec2, get_caller_identity

from bridge.lib.magic_numbers import (
    DEFAULT_POSTGRES_PORT,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
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
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultPostgresDatabaseConfig,
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
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import postgres_role_statements, setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

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

target_vpc_name = jupyterhub_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)
target_vpc_id = target_vpc["id"]

vault_config = Config("vault")

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


rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    jupyterhub_config.get("db_instance_size") or rds_defaults["instance_size"]
)
rds_defaults["use_blue_green"] = False
rds_password = jupyterhub_config.require("rds_password")

jupyterhub_db_security_group = ec2.SecurityGroup(
    f"jupyterhub-db-security-group-{env_name}",
    name=f"jupyterhub-db-{target_vpc_name}-{env_name}",
    description="Access from jupyterhub to its own postgres database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                vault_stack.require_output("vault_server")["security_group"],
            ],
            cidr_blocks=[target_vpc["cidr"]],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to Postgres from jupyterhub nodes.",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Allow k8s cluster ipblocks to talk to DB",
            from_port=DEFAULT_POSTGRES_PORT,
            protocol="tcp",
            security_groups=[],
            to_port=DEFAULT_POSTGRES_PORT,
        ),
    ],
    tags=aws_config.tags,
    vpc_id=target_vpc_id,
)

jupyterhub_db_config = OLPostgresDBConfig(
    instance_name=f"jupyterhub-db-{stack_info.env_suffix}",
    password=rds_password,
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[jupyterhub_db_security_group],
    tags=aws_config.tags,
    db_name="jupyterhub",
    **rds_defaults,
)
jupyterhub_db = OLAmazonDB(jupyterhub_db_config)

jupyterhub_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=jupyterhub_db_config.db_name,
    mount_point=f"{jupyterhub_db_config.engine}-jupyterhub",
    db_admin_username=jupyterhub_db_config.username,
    db_admin_password=rds_password,
    db_host=jupyterhub_db.db_instance.address,
    role_statements=postgres_role_statements,
)
jupyterhub_db_vault_backend = OLVaultDatabaseBackend(
    jupyterhub_db_vault_backend_config, opts=ResourceOptions(depends_on=[jupyterhub_db])
)
jupyterhub_creds_secret_name = "jupyterhub-db-creds"  # noqa: S105  # pragma: allowlist secret

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

app_db_creds_dynamic_secret_config = OLVaultK8SDynamicSecretConfig(
    name="jupyterhub-app-db-creds",
    dest_secret_labels=k8s_global_labels,
    dest_secret_name=jupyterhub_creds_secret_name,
    exclude_raw=True,
    labels=k8s_global_labels,
    mount=jupyterhub_db_vault_backend_config.mount_point,
    namespace=namespace,
    path="creds/app",
    templates={
        "DATABASE_URL": f'postgresql://{{{{ get .Secrets "username" }}}}:{{{{ get .Secrets "password" }}}}@jupyterhub-db-{stack_info.env_suffix}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:{DEFAULT_POSTGRES_PORT}/jupyterhub'
    },
    vaultauth=vault_k8s_resources.auth_name,
)
app_db_creds_dynamic_secret = OLVaultK8SSecret(
    "jupyterhub-app-db-creds-vaultdynamicsecret",
    resource_config=app_db_creds_dynamic_secret_config,
    opts=ResourceOptions(depends_on=jupyterhub_db_vault_backend),
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
        vault_path="sso/mitlearn",
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
                "cull": {
                    "enabled": True,
                    "every": 300,
                    "timeout": 900,
                    "maxAge": 14400,
                    "users": True,
                },
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
                    "podPriority": {"enabled": True},
                    "userPlaceholder": {
                        "enabled": True,
                        "replicas": jupyterhub_config.get_int(
                            "user_placeholder_replicas"
                        )
                        or 4,
                    },
                    "userScheduler": {
                        "enabled": True,
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
                # extraConfig is executed as python at the end of the JH config. For more details see
                # https://z2jh.jupyter.org/en/latest/administrator/advanced.html#hub-extraconfig
                "hub": {
                    "extraEnv": [
                        {
                            "name": "DATABASE_URL",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": jupyterhub_creds_secret_name,
                                    "key": "DATABASE_URL",
                                }
                            },
                        }
                    ],
                    "extraConfig": {
                        "dynamicImageConfig.py": Path(__file__)
                        .parent.joinpath("dynamicImageConfig.py")
                        .read_text()
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
                            "authenticator_class": "tmp",
                        },
                    },
                    "db": {"type": "postgres"},
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
                    "continuous": {
                        "enabled": True,
                    },
                    "extraImages": {
                        # The object keys here are used for RFC 1123 names of init containers.
                        # No underscores are allowed
                        "clustering-and-descriptive-ai": {
                            "name": "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks",
                            "tag": "clustering_and_descriptive_ai",
                        },
                        "deep-learning-foundations-and-applications": {
                            "name": "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks",
                            "tag": "deep_learning_foundations_and_applications",
                        },
                        "introduction-to-data-analytics-and-machine-learning": {
                            "name": "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks",
                            "tag": "introduction_to_data_analytics_and_machine_learning",
                        },
                        "supervised-learning-fundamentals": {
                            "name": "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks",
                            "tag": "supervised_learning_fundamentals",
                        },
                    },
                    "resources": {
                        "requests": {
                            "cpu": "10m",
                            "memory": "10Mi",
                        },
                        "limits": {
                            "cpu": "10m",
                            "memory": "10Mi",
                        },
                    },
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
                    "extraFiles": {
                        "menu_override": {
                            "mountPath": "/opt/conda/share/jupyter/lab/settings/overrides.json",
                            "stringData": Path(__file__)
                            .parent.joinpath("menu_override.json")
                            .read_text(),
                        },
                        "disabled_extensions": {
                            "mountPath": "/home/jovyan/.jupyter/labconfig/page_config.json",
                            "stringData": Path(__file__)
                            .parent.joinpath("disabled_extensions.json")
                            .read_text(),
                        },
                    },
                    "image": {
                        "name": "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks",
                        "tag": "clustering_and_descriptive_ai",
                    },
                    "extraTolerations": [
                        {
                            "key": "ol.mit.edu/gpu_node",
                            "operator": "Equal",
                            "value": "true",
                            "effect": "NoSchedule",
                        }
                    ],
                    "allowPrivilegeEscalation": True,
                    "cmd": [
                        "jupyterhub-singleuser",
                    ],
                    "startTimeout": 600,
                    "networkPolicy": {
                        "enabled": False,
                    },
                    "memory": {
                        "limit": "4G",
                        "guarantee": "1G",
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
        delete_before_replace=True, depends_on=[app_db_creds_dynamic_secret]
    ),
)
