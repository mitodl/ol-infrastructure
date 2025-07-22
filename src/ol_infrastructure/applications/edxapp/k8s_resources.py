# ruff: noqa: F841, E501, PLR0913, ERA001
import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export

from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.applications.edxapp.k8s_configmaps import create_k8s_configmaps
from ol_infrastructure.applications.edxapp.k8s_secrets import create_k8s_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.aws.database import OLAmazonDB
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def create_k8s_resources(
    stack_info: StackInfo,
    cluster_stack: StackReference,
    edxapp_config: Config,
    network_stack: StackReference,
    edxapp_db: OLAmazonDB,
    edxapp_cache: OLAmazonCache,
    aws_config: AWSBase,
    vault_config: Config,
    vault_policy: vault.Policy,
):
    env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
    lms_deployment_name = f"{env_name}-edxapp-lms"
    cms_deployment_name = f"{env_name}-edxapp-cms"

    # Get various VPC / network configuration information
    apps_vpc = network_stack.require_output("applications_vpc")
    k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]

    # Verify that the namespace exists in the EKS cluster
    namespace = f"{stack_info.env_prefix}-openedx"
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(namespace, ns)
    )

    # Look up what what release to deploy for this stack
    release_info = OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)

    # Configure reusable global labels
    k8s_global_labels = K8sGlobalLabels(
        service=Services.edxapp,
        ou=BusinessUnit(stack_info.env_prefix),
        stack=stack_info,
    ).model_dump()

    # Create a kubernetes vault auth backend role
    edxapp_k8s_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"ol-{stack_info.env_prefix}-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name=f"{stack_info.env_prefix}-edxapp",
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=[namespace],
        token_policies=[vault_policy.name],
    )

    # Setup all the bits and bobs needed to interact with vault from k8s
    vault_k8s_resources = OLVaultK8SResources(
        resource_config=OLVaultK8SResourcesConfig(
            application_name=f"{stack_info.env_prefix}-edxapp",
            namespace=namespace,
            labels=k8s_global_labels,
            vault_address=vault_config.require("address"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            vault_auth_role_name=edxapp_k8s_vault_auth_backend_role.role_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[edxapp_k8s_vault_auth_backend_role],
        ),
    )

    # The edxapp application in Kubernetes requires its own security group
    edxapp_k8s_app_security_group = aws.ec2.SecurityGroup(
        f"ol-{stack_info.env_prefix}-edxapp-k8s-app-security-group-{stack_info.env_suffix}",
        name_prefix=f"{env_name}",
        description="Security group for the edxapp application in Kubernetes",
        egress=default_psg_egress_args,
        ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
        tags=aws_config.tags,
        vpc_id=apps_vpc["id"],
    )
    export("edxapp_k8s_app_security_group_id", edxapp_k8s_app_security_group.id)

    # Tell EKS to hook CMS and LMS pods to the application security group
    kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-sg-policy-{stack_info.env_suffix}",
        api_version="vpcresources.k8s.aws/v1beta1",
        kind="SecurityGroupPolicy",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-edxapp-sg-policy",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "podSelector": {
                "matchLabels": {
                    "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
                }
            },
            "securityGroups": {"groupIds": [edxapp_k8s_app_security_group.id]},
        },
        opts=pulumi.ResourceOptions(depends_on=[edxapp_k8s_app_security_group]),
    )

    opensearch_stack = StackReference(
        f"infrastructure.aws.opensearch.{stack_info.env_prefix}.{stack_info.name}"
    )
    opensearch_hostname = opensearch_stack.require_output("cluster")["endpoint"]

    secrets = create_k8s_secrets(
        stack_info,
        namespace,
        k8s_global_labels,
        vault_k8s_resources,
        edxapp_db,
        edxapp_cache,
        edxapp_config,
    )

    configmaps = create_k8s_configmaps(
        stack_info,
        namespace,
        k8s_global_labels,
        edxapp_config,
        edxapp_cache,
        opensearch_hostname,
    )

    ############################################
    # cms deployment resources
    ############################################
    # All of the secrets and configmaps that will be mounted into the edxapp cms containers
    # The names are prefixed with numbers to control the order they are concatenated in.
    cms_edxapp_config_sources = {
        secrets.db_creds_secret_name: secrets.db_creds,
        secrets.db_connections_secret_name: secrets.db_connections,
        secrets.mongo_db_creds_secret_name: secrets.mongo_db_creds,
        secrets.mongo_db_forum_secret_name: secrets.mongo_db_forum,
        secrets.general_secrets_name: secrets.general,
        secrets.xqueue_secret_name: secrets.xqueue,
        secrets.forum_secret_name: secrets.forum,
        configmaps.general_config_name: configmaps.general,
        configmaps.interpolated_config_name: configmaps.interpolated,
        # Just CMS specific resources below this line
        secrets.cms_oauth_secret_name: secrets.cms_oauth,
        configmaps.cms_general_config_name: configmaps.cms_general,
        configmaps.cms_interpolated_config_name: configmaps.cms_interpolated,
    }
    cms_edxapp_secret_names = [
        secrets.db_creds_secret_name,
        secrets.db_connections_secret_name,
        secrets.mongo_db_creds_secret_name,
        secrets.mongo_db_forum_secret_name,
        secrets.general_secrets_name,
        secrets.xqueue_secret_name,
        secrets.forum_secret_name,
        # Just CMS specific resources below this line
        secrets.cms_oauth_secret_name,
    ]
    cms_edxapp_configmap_names = [
        configmaps.general_config_name,
        configmaps.interpolated_config_name,
        # Just CMS specific resources below this line
        configmaps.cms_general_config_name,
        configmaps.cms_interpolated_config_name,
    ]

    # Define the volumes that will be mounted into the edxapp containers
    cms_edxapp_volumes = [
        kubernetes.core.v1.VolumeArgs(
            name=secret_name,
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(secret_name=secret_name),
        )
        for secret_name in cms_edxapp_secret_names
    ]
    cms_edxapp_volumes.extend(
        [
            kubernetes.core.v1.VolumeArgs(
                name=configmap_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmap_name
                ),
            )
            for configmap_name in cms_edxapp_configmap_names
        ]
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="edxapp-config",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-data",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-logs",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-media",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name=configmaps.uwsgi_ini_config_name,
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=configmaps.uwsgi_ini_config_name
            ),
        )
    )

    # Define the volume mounts for the init container that aggregates the config files
    cms_edxapp_init_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name=source_name,
            mount_path=f"/openedx/config-sources/{source_name}",
            read_only=True,
        )
        for source_name in cms_edxapp_config_sources
    ]
    cms_edxapp_init_volume_mounts.append(
        kubernetes.core.v1.VolumeMountArgs(
            name="edxapp-config",
            mount_path="/openedx/config",
        )
    )

    cms_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-cms",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    cms_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-cms-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=cms_deployment_name,
            namespace=namespace,
            labels=cms_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(match_labels=cms_labels),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=cms_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=cms_edxapp_volumes,
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image="busybox:1.35",
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/cms.env.yml"
                            ],
                            volume_mounts=cms_edxapp_init_volume_mounts,
                        )
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="cms-edxapp",
                            # command=["/bin/sh", "-c", "sleep infinity"],
                            # image="busybox:1.35",
                            image="610119931565.dkr.ecr.us-east-1.amazonaws.com/dockerhub/mitodl/edxapp:master-mitxonline-36327ff",
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SERVICE_VARIANT", value="cms"
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DJANGO_SETTINGS_MODULE",
                                    value="cms.envs.production",
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="UWSGI_WORKERS", value="2"
                                ),
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="edxapp-config",
                                    mount_path="/openedx/config",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-data",
                                    mount_path="/openedx/data",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-logs",
                                    mount_path="/openedx/data/logs",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-media",
                                    mount_path="/openedx/media/",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name=configmaps.uwsgi_ini_config_name,
                                    mount_path="/openedx/edx-platform/uwsgi.ini",
                                    sub_path="uwsgi.ini",
                                ),
                            ],
                        )
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=list(cms_edxapp_config_sources.values())
        ),
    )

    ############################################
    # lms deployment resources
    ############################################
    lms_edxapp_config_sources = {
        secrets.db_creds_secret_name: secrets.db_creds,
        secrets.db_connections_secret_name: secrets.db_connections,
        secrets.mongo_db_creds_secret_name: secrets.mongo_db_creds,
        secrets.mongo_db_forum_secret_name: secrets.mongo_db_forum,
        secrets.general_secrets_name: secrets.general,
        secrets.xqueue_secret_name: secrets.xqueue,
        secrets.forum_secret_name: secrets.forum,
        configmaps.general_config_name: configmaps.general,
        configmaps.interpolated_config_name: configmaps.interpolated,
        # Just LMS specific resources below this line
        secrets.lms_oauth_secret_name: secrets.lms_oauth,
        configmaps.lms_interpolated_config_name: configmaps.lms_interpolated,
    }
    lms_edxapp_secret_names = [
        secrets.db_creds_secret_name,
        secrets.db_connections_secret_name,
        secrets.mongo_db_creds_secret_name,
        secrets.mongo_db_forum_secret_name,
        secrets.general_secrets_name,
        secrets.xqueue_secret_name,
        secrets.forum_secret_name,
        # Just LMS specific resources below this line
        secrets.lms_oauth_secret_name,
    ]
    lms_edxapp_configmap_names = [
        configmaps.general_config_name,
        configmaps.interpolated_config_name,
        # Just LMS specific resources below this line
        configmaps.lms_interpolated_config_name,
    ]

    # Define the volumes that will be mounted into the edxapp containers
    lms_edxapp_volumes = [
        kubernetes.core.v1.VolumeArgs(
            name=secret_name,
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(secret_name=secret_name),
        )
        for secret_name in lms_edxapp_secret_names
    ]
    lms_edxapp_volumes.extend(
        [
            kubernetes.core.v1.VolumeArgs(
                name=configmap_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmap_name
                ),
            )
            for configmap_name in lms_edxapp_configmap_names
        ]
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="edxapp-config",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-data",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-logs",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-media",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name=configmaps.uwsgi_ini_config_name,
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=configmaps.uwsgi_ini_config_name
            ),
        )
    )

    # Define the volume mounts for the init container that aggregates the config files
    lms_edxapp_init_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name=source_name,
            mount_path=f"/openedx/config-sources/{source_name}",
            read_only=True,
        )
        for source_name in lms_edxapp_config_sources
    ]
    lms_edxapp_init_volume_mounts.append(
        kubernetes.core.v1.VolumeMountArgs(
            name="edxapp-config",
            mount_path="/openedx/config",
        )
    )

    lms_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-lms",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    lms_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-lms-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=lms_deployment_name,
            namespace=namespace,
            labels=lms_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(match_labels=lms_labels),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=lms_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=lms_edxapp_volumes,
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image="busybox:1.35",
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml"
                            ],
                            volume_mounts=lms_edxapp_init_volume_mounts,
                        )
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="lms-edxapp",
                            # image="busybox:1.35",
                            # command=["/bin/sh", "-c", "sleep infinity"],
                            image="610119931565.dkr.ecr.us-east-1.amazonaws.com/dockerhub/mitodl/edxapp:master-mitxonline-36327ff",
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SERVICE_VARIANT", value="lms"
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DJANGO_SETTINGS_MODULE",
                                    value="lms.envs.production",
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="UWSGI_WORKERS", value="2"
                                ),
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="edxapp-config",
                                    mount_path="/openedx/config",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-data",
                                    mount_path="/openedx/data",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-logs",
                                    mount_path="/openedx/data/logs",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-media",
                                    mount_path="/openedx/media/",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name=configmaps.uwsgi_ini_config_name,
                                    mount_path="/openedx/edx-platform/uwsgi.ini",
                                    sub_path="uwsgi.ini",
                                ),
                            ],
                        )
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=list(lms_edxapp_config_sources.values())
        ),
    )

    return {
        "edxapp_k8s_app_security_group_id": edxapp_k8s_app_security_group.id,
    }
