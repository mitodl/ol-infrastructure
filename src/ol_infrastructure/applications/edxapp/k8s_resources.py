# ruff: noqa: F841, E501, PLR0912, PLR0913, PLR0915
"""Kubernetes resources for the edxapp application."""

import os
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import iam

from bridge.settings.openedx.types import OpenEdxSupportedRelease
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.applications.edxapp.k8s_autoscaling import (
    build_cms_webapp_keda_config,
    build_lms_webapp_keda_config,
    create_celery_autoscaling_resources,
    create_webapp_trigger_auth,
)
from ol_infrastructure.applications.edxapp.k8s_configmaps import (
    create_k8s_configmaps,
)
from ol_infrastructure.applications.edxapp.k8s_secrets import create_k8s_secrets
from ol_infrastructure.applications.edxapp.meilisearch import (
    create_meilisearch_resources,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.aws.database import OLAmazonDB
from ol_infrastructure.components.aws.eks import (
    OLEKSTrustRole,
    OLEKSTrustRoleConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    GranianConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
    OLApplicationK8s,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
)
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sAppLabels,
    K8sGlobalLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def create_k8s_resources(  # noqa: C901
    aws_config: AWSBase,
    cluster_stack: StackReference,
    edxapp_cache: OLAmazonCache,
    edxapp_config: Config,
    edxapp_db: OLAmazonDB,
    edxapp_iam_policy: aws.iam.Policy,
    mongodb_atlas_stack: StackReference,
    network_stack: StackReference,
    notes_stack: StackReference,
    stack_info: StackInfo,
    vault_config: Config,
    vault_policy: vault.Policy,
) -> None:
    """Create all Kubernetes resources for the edxapp LMS and CMS deployments."""
    env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
    lms_webapp_deployment_name = f"{env_name}-edxapp-lms-webapp"
    cms_webapp_deployment_name = f"{env_name}-edxapp-cms-webapp"

    lms_celery_deployment_name = f"{env_name}-edxapp-lms-celery"
    cms_celery_deployment_name = f"{env_name}-edxapp-cms-celery"

    aws_account = aws.get_caller_identity()

    replicas_dict = edxapp_config.require_object("k8s_replicas")
    resources_dict = edxapp_config.require_object("k8s_resources")

    # Get various VPC / network configuration information
    data_vpc = network_stack.require_output("data_vpc")
    operations_vpc = network_stack.require_output("operations_vpc")
    edxapp_target_vpc = (
        edxapp_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
    )
    edxapp_vpc = network_stack.require_output(edxapp_target_vpc)

    # For K8s deployments, edxapp uses the applications_vpc cluster
    # (not the target_vpc which is for EC2 deployments)
    cluster_vpc = network_stack.require_output(edxapp_config.require("k8s_vpc"))
    k8s_pod_subnet_cidrs = cluster_vpc["k8s_pod_subnet_cidrs"]

    # Verify that the namespace exists in the EKS cluster
    namespace = f"{stack_info.env_prefix}-openedx"
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(namespace, ns)
    )

    # Look up what what release to deploy for this stack
    release_info = OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)

    opensearch_stack = StackReference(
        f"infrastructure.aws.opensearch.{stack_info.env_prefix}.{stack_info.name}"
    )
    opensearch_hostname = opensearch_stack.require_output("cluster")["endpoint"]

    # Configure reusable global labels
    ou = BusinessUnit(edxapp_config.require("business_unit"))
    k8s_global_labels = K8sAppLabels(
        product=Product[edxapp_config.require("product")],
        service=Services.openedx,
        application=Application.openedx_platform,
        ou=ou,
        stack=stack_info,
        source_repository="https://github.com/openedx/openedx-platform",
    ).model_dump()

    # Create ConfigMap for Vector configuration
    vector_config_path = Path(__file__).parent / "files/vector/edxapp_tracking_log.yaml"
    with vector_config_path.open() as f:
        vector_config_content = f.read()

    vector_configmap = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-vector-config-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-edxapp-vector-config",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        data={
            "vector.yaml": vector_config_content,
        },
    )

    # We need to "guess" service account name here because there is a loop with
    # needing the service account name for the trust role, but needing the trustrole
    # for the annotations on the service account created by OLVaultK8SResources.
    edxapp_service_account_name = f"{stack_info.env_prefix}-edxapp-vault"

    edxapp_trust_role = OLEKSTrustRole(
        f"ol-{stack_info.env_prefix}-edxapp-trustrole-{stack_info.env_suffix}",
        role_config=OLEKSTrustRoleConfig(
            account_id=aws_account.account_id,
            cluster_name=f"applications-{stack_info.name}",
            cluster_identities=cluster_stack.require_output("cluster_identities"),
            description=f"Trust role for allowing the {env_name} edxapp "
            "application to access AWS resources",
            policy_operator="StringEquals",
            role_name=f"edxapp-trustrole-{env_name}",
            service_account_identifier=(
                f"system:serviceaccount:{namespace}:{edxapp_service_account_name}"
            ),
            tags=aws_config.tags,
        ),
    )

    iam.RolePolicyAttachment(
        f"ol-{stack_info.env_prefix}-edxapp-trustrole-policy-attachment-{stack_info.env_suffix}",
        policy_arn=edxapp_iam_policy.arn,
        role=edxapp_trust_role.role.name,
    )

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
            annotations={"eks.amazonaws.com/role-arn": edxapp_trust_role.role.arn},
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
        vpc_id=cluster_vpc["id"],
    )
    export("edxapp_k8s_app_security_group_id", edxapp_k8s_app_security_group.id)

    # Tell EKS to hook all edxapp pods to the application security group.
    # The component creates its own SecurityGroupPolicies for LMS and CMS webapp pods;
    # this hand-rolled policy covers celery and lms-process-scheduled-emails pods.
    # Using the SG ID as the label value (matching what the component injects) ensures
    # a single shared label across all edxapp workloads.
    edxapp_celery_sg_policy = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-celery-sg-policy-{stack_info.env_suffix}",
        api_version="vpcresources.k8s.aws/v1beta1",
        kind="SecurityGroupPolicy",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-edxapp-celery-sg-policy",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "podSelector": {
                "matchLabels": {
                    "ol.mit.edu/edxapp-celery-sg": "true",
                }
            },
            "securityGroups": {"groupIds": [edxapp_k8s_app_security_group.id]},
        },
        opts=pulumi.ResourceOptions(depends_on=[edxapp_k8s_app_security_group]),
    )

    # Call out to other modules to create the k8s secrets and configmaps
    secrets = create_k8s_secrets(
        edxapp_cache=edxapp_cache,
        edxapp_config=edxapp_config,
        edxapp_db=edxapp_db,
        k8s_global_labels=k8s_global_labels,
        mongodb_atlas_stack=mongodb_atlas_stack,
        namespace=namespace,
        stack_info=stack_info,
        vault_k8s_resources=vault_k8s_resources,
    )
    configmaps = create_k8s_configmaps(
        stack_info=stack_info,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        edxapp_config=edxapp_config,
        edxapp_cache=edxapp_cache,
        notes_stack=notes_stack,
        opensearch_hostname=opensearch_hostname,
    )

    openedx_data_pvc = kubernetes.core.v1.PersistentVolumeClaim(
        f"ol-{stack_info.env_prefix}-openedx-data-pvc-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-openedx-data-pvc",
            namespace=namespace,
            labels=K8sGlobalLabels(
                service=Services.edxapp,
                application=stack_info.env_prefix,
                ou=ou,
                stack=stack_info,
                source_repository="https://github.com/openedx/openedx-platform",
            ).model_dump(),
        ),
        spec=kubernetes.core.v1.PersistentVolumeClaimSpecArgs(
            access_modes=["ReadWriteMany"],
            storage_class_name="efs-sc",
            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                requests={"storage": "5Gi"}
            ),
        ),
        opts=pulumi.ResourceOptions(ignore_changes=["metadata.labels"]),
    )

    # Lookup environment vars needed for the deployment
    if "EDXAPP_DOCKER_IMAGE_DIGEST" not in os.environ:
        msg = "Environment variable EDXAPP_DOCKER_IMAGE_DIGEST is not set. "
        raise OSError(msg)
    EDXAPP_DOCKER_IMAGE_DIGEST = os.environ["EDXAPP_DOCKER_IMAGE_DIGEST"]

    OPENEDX_RELEASE: OpenEdxSupportedRelease = os.environ.get(
        "OPENEDX_RELEASE", "master"
    )

    ############################################
    # Shared container and volume definitions
    ############################################

    # Init container that ensures the export_course_repos directory exists.
    # Volume mounts are injected by the OLApplicationK8s component via extra_volume_mounts.
    export_course_repos_init_container = kubernetes.core.v1.ContainerArgs(
        name="export-course-repos-mkdir",
        image=cached_image_uri("busybox:1.35"),
        command=["/bin/sh", "-c", "mkdir -p /openedx/data/export_course_repos"],
    )

    # Common volume mounts for main application containers (both webapp and celery).
    # These are injected by the component into all containers via extra_volume_mounts.
    common_extra_volume_mounts = [
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
            name=configmaps.waffle_flags_yaml_config_name,
            mount_path="/openedx/config/waffle-flags.yaml",
            sub_path="waffle-flags.yaml",
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name=secrets.git_export_ssh_key_secret_name,
            mount_path="/openedx/.ssh/id_rsa",
            sub_path="private_key",
            read_only=True,
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name=configmaps.uwsgi_ini_config_name,
            mount_path="/openedx/edx-platform/uwsgi.ini",
            sub_path="uwsgi.ini",
        ),
    ]

    # The Vector log-shipping sidecar mounts differ per service (lms vs cms log paths).
    def _make_vector_sidecar(service: str) -> kubernetes.core.v1.ContainerArgs:
        return kubernetes.core.v1.ContainerArgs(
            name="vector",
            image="timberio/vector:0.34.1-alpine",
            security_context=kubernetes.core.v1.SecurityContextArgs(
                run_as_group=0,
                run_as_user=0,
            ),
            env=[
                kubernetes.core.v1.EnvVarArgs(
                    name="ENVIRONMENT",
                    value=stack_info.env_prefix,
                ),
                kubernetes.core.v1.EnvVarArgs(
                    name="TIER",
                    value=stack_info.env_suffix,
                ),
            ],
            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                requests={"cpu": "100m", "memory": "512Mi"},
                limits={"memory": "512Mi"},
            ),
            volume_mounts=[
                kubernetes.core.v1.VolumeMountArgs(
                    name="openedx-logs",
                    mount_path=f"/opt/data/{service}/logs",
                    read_only=True,
                ),
                kubernetes.core.v1.VolumeMountArgs(
                    name="vector-config",
                    mount_path="/etc/vector",
                    read_only=True,
                ),
            ],
            args=["--config", "/etc/vector/vector.yaml"],
        )

    pod_security_context = kubernetes.core.v1.PodSecurityContextArgs(
        run_as_user=1000,
        run_as_group=1000,
        fs_group=1000,
    )

    log_level = edxapp_config.get("log_level") or "warn"

    ############################################
    # Webapp KEDA TriggerAuthentication
    # Must be created before OLApplicationK8s instances so the name is known.
    ############################################
    webapp_trigger_auth, trigger_auth_name = create_webapp_trigger_auth(
        env_name=env_name,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        stack_info=stack_info,
        vault_k8s_resources=vault_k8s_resources,
    )

    lms_webapp_keda_config = build_lms_webapp_keda_config(
        trigger_auth_name=trigger_auth_name,
        stack_info=stack_info,
        edxapp_config=edxapp_config,
    )
    cms_webapp_keda_config = build_cms_webapp_keda_config(
        trigger_auth_name=trigger_auth_name,
        stack_info=stack_info,
        edxapp_config=edxapp_config,
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
        secrets.forum_secret_name: secrets.forum,
        secrets.learn_ai_canvas_syllabus_token_secret_name: secrets.learn_ai_canvas_syllabus_token,
        configmaps.general_config_name: configmaps.general,
        configmaps.interpolated_config_name: configmaps.interpolated,
        configmaps.lms_general_config_name: configmaps.lms_general,
        configmaps.lms_interpolated_config_name: configmaps.lms_interpolated,
    }
    if secrets.xqueue:
        lms_edxapp_config_sources[secrets.xqueue_secret_name] = secrets.xqueue
    if secrets.lms_oauth:
        lms_edxapp_config_sources[secrets.lms_oauth_secret_name] = secrets.lms_oauth
    if secrets.translations_providers:
        lms_edxapp_config_sources[secrets.translations_providers_secret_name] = (
            secrets.translations_providers
        )
    lms_edxapp_secret_names = [
        secrets.db_creds_secret_name,
        secrets.db_connections_secret_name,
        secrets.mongo_db_creds_secret_name,
        secrets.mongo_db_forum_secret_name,
        secrets.general_secrets_name,
        secrets.forum_secret_name,
        secrets.learn_ai_canvas_syllabus_token_secret_name,
        secrets.git_export_ssh_key_secret_name,
    ]
    if secrets.xqueue:
        lms_edxapp_secret_names.append(secrets.xqueue_secret_name)
    if secrets.lms_oauth:
        lms_edxapp_secret_names.append(secrets.lms_oauth_secret_name)
    if secrets.translations_providers:
        lms_edxapp_secret_names.append(secrets.translations_providers_secret_name)
    lms_edxapp_configmap_names = [
        configmaps.general_config_name,
        configmaps.interpolated_config_name,
        configmaps.lms_general_config_name,
        configmaps.lms_interpolated_config_name,
    ]

    lms_edxapp_volumes = [
        kubernetes.core.v1.VolumeArgs(
            name=secret_name,
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                secret_name=secret_name,
                default_mode=0o600
                if secret_name == secrets.git_export_ssh_key_secret_name
                else None,
            ),
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
    lms_edxapp_volumes.extend(
        [
            kubernetes.core.v1.VolumeArgs(
                name="edxapp-config",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
            ),
            kubernetes.core.v1.VolumeArgs(
                name="openedx-data",
                persistent_volume_claim=kubernetes.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                    claim_name=openedx_data_pvc.metadata.name
                ),
            ),
            kubernetes.core.v1.VolumeArgs(
                name="openedx-logs",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
            ),
            kubernetes.core.v1.VolumeArgs(
                name="openedx-media",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
            ),
            kubernetes.core.v1.VolumeArgs(
                name=configmaps.uwsgi_ini_config_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmaps.uwsgi_ini_config_name
                ),
            ),
            kubernetes.core.v1.VolumeArgs(
                name=configmaps.waffle_flags_yaml_config_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmaps.waffle_flags_yaml_config_name,
                ),
            ),
            kubernetes.core.v1.VolumeArgs(
                name="vector-config",
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=f"{env_name}-edxapp-vector-config",
                ),
            ),
        ]
    )

    # Mounts injected into init containers only: the config source paths that
    # the config-aggregator uses to concatenate config YAMLs.
    lms_edxapp_init_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name=source_name,
            mount_path=f"/openedx/config-sources/{source_name}",
            read_only=True,
        )
        for source_name in lms_edxapp_config_sources
    ]

    # Config aggregator for LMS: concatenates all config sources into lms.env.yml.
    # All mounts come via extra_volume_mounts + extra_init_volume_mounts injection.
    lms_config_aggregator_init_container = kubernetes.core.v1.ContainerArgs(
        name="config-aggregator",
        image=cached_image_uri("busybox:1.35"),
        command=["/bin/sh", "-c"],
        args=["cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml"],
    )

    lms_app = OLApplicationK8s(
        OLApplicationK8sConfig(
            application_name="lms-edxapp",
            application_namespace=namespace,
            application_image_repository="mitodl/edxapp",
            application_image_digest=EDXAPP_DOCKER_IMAGE_DIGEST,
            application_config={
                "SERVICE_VARIANT": "lms",
                "DJANGO_SETTINGS_MODULE": "lms.envs.production",
                "UWSGI_WORKERS": "2",
            },
            application_lb_service_name=lms_webapp_deployment_name,
            application_lb_service_port_name="http",
            application_port=8000,
            application_min_replicas=replicas_dict["webapp"]["lms"]["min"],
            application_max_replicas=replicas_dict["webapp"]["lms"]["max"],
            application_security_group_name=edxapp_k8s_app_security_group.id,
            application_security_group_id=edxapp_k8s_app_security_group.id,
            application_service_account_name=vault_k8s_resources.service_account_name,
            vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
            k8s_global_labels=k8s_global_labels,
            env_from_secret_names=[],
            project_root=Path(__file__).parent,
            import_nginx_config=False,
            import_uwsgi_config=False,
            init_migrations=False,
            init_collectstatic=False,
            granian_config=GranianConfig(
                application_module="lms.wsgi:application",
                port=8000,
                workers=2,
                no_ws=True,
                runtime_mode="mt",
                runtime_threads=2,
                respawn_failed_workers=True,
                backlog=128,
                static_path_mounts=["/openedx/staticfiles"],
                log_level=log_level,
                limit_workers_max_rss=True,
            ),
            resource_requests={
                "cpu": resources_dict["webapp"]["lms"]["cpu_request"],
                "memory": resources_dict["webapp"]["lms"]["memory_request"],
            },
            resource_limits={
                "memory": resources_dict["webapp"]["lms"]["memory_limit"],
            },
            pod_security_context=pod_security_context,
            extra_volumes=lms_edxapp_volumes,
            extra_volume_mounts=common_extra_volume_mounts,
            extra_init_volume_mounts=lms_edxapp_init_volume_mounts,
            extra_init_containers=[
                export_course_repos_init_container,
                lms_config_aggregator_init_container,
            ],
            extra_sidecar_containers=[_make_vector_sidecar("lms")],
            pre_deploy_commands=[
                (
                    "lms-migrate",
                    ["python", "manage.py", "lms", "migrate", "--noinput"],
                ),
                (
                    "lms-waffleflag",
                    [
                        "python",
                        "set_waffle_flags.py",
                        "/openedx/config/waffle-flags.yaml",
                    ],
                ),
            ],
            webapp_keda_config=lms_webapp_keda_config,
            webapp_deployment_aliases=[
                pulumi.Alias(
                    name=f"ol-{stack_info.env_prefix}-edxapp-lms-deployment-{stack_info.env_suffix}",
                    parent=pulumi.ROOT_STACK_RESOURCE,
                )
            ],
            webapp_service_aliases=[
                pulumi.Alias(
                    name=f"ol-{stack_info.env_prefix}-edxapp-lms-service-{stack_info.env_suffix}",
                    parent=pulumi.ROOT_STACK_RESOURCE,
                )
            ],
            webapp_keda_aliases=[
                pulumi.Alias(
                    name=f"ol-{stack_info.env_prefix}-edxapp-lms-scaledobject-{stack_info.env_suffix}",
                    parent=pulumi.ROOT_STACK_RESOURCE,
                )
            ],
        ),
        opts=ResourceOptions(
            depends_on=[
                *[v for v in lms_edxapp_config_sources.values() if v is not None],
                vector_configmap,
                webapp_trigger_auth,
            ]
        ),
    )

    ############################################
    # cms deployment resources
    ############################################
    cms_edxapp_config_sources = {
        secrets.db_creds_secret_name: secrets.db_creds,
        secrets.db_connections_secret_name: secrets.db_connections,
        secrets.mongo_db_creds_secret_name: secrets.mongo_db_creds,
        secrets.mongo_db_forum_secret_name: secrets.mongo_db_forum,
        secrets.general_secrets_name: secrets.general,
        secrets.forum_secret_name: secrets.forum,
        configmaps.general_config_name: configmaps.general,
        configmaps.interpolated_config_name: configmaps.interpolated,
        secrets.cms_oauth_secret_name: secrets.cms_oauth,
        configmaps.cms_general_config_name: configmaps.cms_general,
        configmaps.cms_interpolated_config_name: configmaps.cms_interpolated,
    }
    if secrets.xqueue:
        cms_edxapp_config_sources[secrets.xqueue_secret_name] = secrets.xqueue
    if secrets.translations_providers:
        cms_edxapp_config_sources[secrets.translations_providers_secret_name] = (
            secrets.translations_providers
        )
    if secrets.meilisearch:
        cms_edxapp_config_sources[secrets.meilisearch_secret_name] = secrets.meilisearch
    cms_edxapp_secret_names = [
        secrets.db_creds_secret_name,
        secrets.db_connections_secret_name,
        secrets.mongo_db_creds_secret_name,
        secrets.mongo_db_forum_secret_name,
        secrets.general_secrets_name,
        secrets.forum_secret_name,
        secrets.learn_ai_canvas_syllabus_token_secret_name,
        secrets.git_export_ssh_key_secret_name,
        secrets.cms_oauth_secret_name,
    ]
    if secrets.xqueue:
        cms_edxapp_secret_names.append(secrets.xqueue_secret_name)
    if secrets.translations_providers:
        cms_edxapp_secret_names.append(secrets.translations_providers_secret_name)
    if secrets.meilisearch:
        cms_edxapp_secret_names.append(secrets.meilisearch_secret_name)
    cms_edxapp_configmap_names = [
        configmaps.general_config_name,
        configmaps.interpolated_config_name,
        configmaps.cms_general_config_name,
        configmaps.cms_interpolated_config_name,
    ]

    cms_edxapp_volumes = [
        kubernetes.core.v1.VolumeArgs(
            name=secret_name,
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                secret_name=secret_name,
                default_mode=0o600
                if secret_name == secrets.git_export_ssh_key_secret_name
                else None,
            ),
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
    cms_edxapp_volumes.extend(
        [
            kubernetes.core.v1.VolumeArgs(
                name="edxapp-config",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
            ),
            kubernetes.core.v1.VolumeArgs(
                name="openedx-data",
                persistent_volume_claim=kubernetes.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                    claim_name=openedx_data_pvc.metadata.name
                ),
            ),
            kubernetes.core.v1.VolumeArgs(
                name="openedx-logs",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
            ),
            kubernetes.core.v1.VolumeArgs(
                name="openedx-media",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
            ),
            kubernetes.core.v1.VolumeArgs(
                name=configmaps.uwsgi_ini_config_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmaps.uwsgi_ini_config_name
                ),
            ),
            kubernetes.core.v1.VolumeArgs(
                name=configmaps.waffle_flags_yaml_config_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmaps.waffle_flags_yaml_config_name,
                ),
            ),
            kubernetes.core.v1.VolumeArgs(
                name="vector-config",
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=f"{env_name}-edxapp-vector-config",
                ),
            ),
        ]
    )

    cms_edxapp_init_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name=source_name,
            mount_path=f"/openedx/config-sources/{source_name}",
            read_only=True,
        )
        for source_name in cms_edxapp_config_sources
    ]

    cms_config_aggregator_init_container = kubernetes.core.v1.ContainerArgs(
        name="config-aggregator",
        image=cached_image_uri("busybox:1.35"),
        command=["/bin/sh", "-c"],
        args=["cat /openedx/config-sources/*/*.yaml > /openedx/config/cms.env.yml"],
    )

    cms_app = OLApplicationK8s(
        OLApplicationK8sConfig(
            application_name="cms-edxapp",
            application_namespace=namespace,
            application_image_repository="mitodl/edxapp",
            application_image_digest=EDXAPP_DOCKER_IMAGE_DIGEST,
            application_config={
                "SERVICE_VARIANT": "cms",
                "DJANGO_SETTINGS_MODULE": "cms.envs.production",
                "UWSGI_WORKERS": "2",
            },
            application_lb_service_name=cms_webapp_deployment_name,
            application_lb_service_port_name="http",
            application_port=8000,
            application_min_replicas=replicas_dict["webapp"]["cms"]["min"],
            application_max_replicas=replicas_dict["webapp"]["cms"]["max"],
            application_security_group_name=edxapp_k8s_app_security_group.id,
            application_security_group_id=edxapp_k8s_app_security_group.id,
            application_service_account_name=vault_k8s_resources.service_account_name,
            vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
            k8s_global_labels=k8s_global_labels,
            env_from_secret_names=[],
            project_root=Path(__file__).parent,
            import_nginx_config=False,
            import_uwsgi_config=False,
            init_migrations=False,
            init_collectstatic=False,
            granian_config=GranianConfig(
                application_module="cms.wsgi:application",
                port=8000,
                workers=2,
                no_ws=True,
                runtime_mode="mt",
                runtime_threads=2,
                respawn_failed_workers=True,
                backlog=128,
                static_path_mounts=["/openedx/staticfiles"],
                log_level=log_level,
                limit_workers_max_rss=True,
            ),
            resource_requests={
                "cpu": resources_dict["webapp"]["cms"]["cpu_request"],
                "memory": resources_dict["webapp"]["cms"]["memory_request"],
            },
            resource_limits={
                "memory": resources_dict["webapp"]["cms"]["memory_limit"],
            },
            pod_security_context=pod_security_context,
            extra_volumes=cms_edxapp_volumes,
            extra_volume_mounts=common_extra_volume_mounts,
            extra_init_volume_mounts=cms_edxapp_init_volume_mounts,
            extra_init_containers=[
                export_course_repos_init_container,
                cms_config_aggregator_init_container,
            ],
            extra_sidecar_containers=[_make_vector_sidecar("cms")],
            pre_deploy_commands=[
                (
                    "cms-migrate",
                    ["python", "manage.py", "cms", "migrate", "--noinput"],
                ),
            ],
            webapp_keda_config=cms_webapp_keda_config,
            webapp_deployment_aliases=[
                pulumi.Alias(
                    name=f"ol-{stack_info.env_prefix}-edxapp-cms-deployment-{stack_info.env_suffix}",
                    parent=pulumi.ROOT_STACK_RESOURCE,
                )
            ],
            webapp_service_aliases=[
                pulumi.Alias(
                    name=f"ol-{stack_info.env_prefix}-edxapp-cms-service-{stack_info.env_suffix}",
                    parent=pulumi.ROOT_STACK_RESOURCE,
                )
            ],
            webapp_keda_aliases=[
                pulumi.Alias(
                    name=f"ol-{stack_info.env_prefix}-edxapp-cms-scaledobject-{stack_info.env_suffix}",
                    parent=pulumi.ROOT_STACK_RESOURCE,
                )
            ],
        ),
        opts=ResourceOptions(
            depends_on=[
                *[v for v in cms_edxapp_config_sources.values() if v is not None],
                vector_configmap,
                webapp_trigger_auth,
                lms_app,  # CMS migration waits for LMS to be fully deployed
            ]
        ),
    )

    ############################################
    # Hand-rolled celery deployments
    # The celery command format (--app=, --hostname, --exclude-queues) is
    # incompatible with OLApplicationK8sCeleryWorkerConfig. These remain
    # as hand-rolled deployments and are scaled via external KEDA ScaledObjects.
    ############################################

    # Common volume mounts for celery containers (no uwsgi.ini needed)
    celery_volume_mounts = common_extra_volume_mounts[:-1]  # exclude uwsgi.ini

    celery_env_vars = [
        kubernetes.core.v1.EnvVarArgs(
            name="CELERY_TASK_ACKS_LATE",
            value="True",
        ),
        kubernetes.core.v1.EnvVarArgs(
            name="CELERY_TASK_REJECT_ON_WORKER_LOST",
            value="True",
        ),
    ]

    # Selector labels must match the existing Deployment's spec.selector (immutable).
    # The old SGP label (pod-security-group) is kept in the selector; the new
    # edxapp-celery-sg label is added only to the pod template so the
    # edxapp_celery_sg_policy SGP can select celery pods without altering the selector.
    lms_celery_selector_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-lms-celery",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    lms_celery_labels = lms_celery_selector_labels | {
        "ol.mit.edu/edxapp-celery-sg": "true",
    }
    lms_celery_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-lms-celery-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{lms_celery_deployment_name}",
            namespace=namespace,
            labels=lms_celery_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=lms_celery_selector_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=lms_celery_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    security_context=pod_security_context,
                    volumes=lms_edxapp_volumes,
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="export-course-repos-mkdir",
                            image=cached_image_uri("busybox:1.35"),
                            command=[
                                "/bin/sh",
                                "-c",
                                "mkdir -p /openedx/data/export_course_repos",
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-data",
                                    mount_path="/openedx/data",
                                ),
                            ],
                        ),
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image=cached_image_uri("busybox:1.35"),
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml"
                            ],
                            volume_mounts=[
                                *lms_edxapp_init_volume_mounts,
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="edxapp-config",
                                    mount_path="/openedx/config",
                                ),
                            ],
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="lms-edxapp",
                            image=cached_image_uri(
                                f"mitodl/edxapp@{EDXAPP_DOCKER_IMAGE_DIGEST}"
                            ),
                            command=["celery"],
                            args=[
                                "--app=lms.celery",
                                "worker",
                                "-B",
                                "-E",
                                "--loglevel=info",
                                "--hostname=edx.lms.core.default.%h",
                                "--max-tasks-per-child",
                                "100",
                                "--queues=edx.lms.core.default,edx.lms.core.high,edx.lms.core.high_mem",
                                "--exclude-queues=edx.cms.core.default",
                                "--concurrency=2",
                                "--prefetch-multiplier=1",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SERVICE_VARIANT", value="lms"
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DJANGO_SETTINGS_MODULE",
                                    value="lms.envs.production",
                                ),
                                *celery_env_vars,
                            ],
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={
                                    "cpu": resources_dict["celery"]["lms"][
                                        "cpu_request"
                                    ],
                                    "memory": resources_dict["celery"]["lms"][
                                        "memory_request"
                                    ],
                                },
                                limits={
                                    "memory": resources_dict["celery"]["lms"][
                                        "memory_limit"
                                    ],
                                },
                            ),
                            volume_mounts=celery_volume_mounts,
                        )
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=[v for v in lms_edxapp_config_sources.values() if v is not None]
        ),
    )

    # Special one-off deployment that invokes the process_scheduled_emails.py script
    lms_process_scheduled_emails_selector_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-lms-process-scheduled-emails",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    lms_process_scheduled_emails_labels = (
        lms_process_scheduled_emails_selector_labels
        | {
            "ol.mit.edu/edxapp-celery-sg": "true",
        }
    )
    lms_process_scheduled_emails_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-lms-process-scheduled-emails-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-edxapp-lms-process-scheduled-emails",
            namespace=namespace,
            labels=lms_process_scheduled_emails_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=lms_process_scheduled_emails_selector_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=lms_process_scheduled_emails_labels
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    security_context=pod_security_context,
                    volumes=lms_edxapp_volumes,
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="export-course-repos-mkdir",
                            image=cached_image_uri("busybox:1.35"),
                            command=[
                                "/bin/sh",
                                "-c",
                                "mkdir -p /openedx/data/export_course_repos",
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-data",
                                    mount_path="/openedx/data",
                                ),
                            ],
                        ),
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image=cached_image_uri("busybox:1.35"),
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml"
                            ],
                            volume_mounts=[
                                *lms_edxapp_init_volume_mounts,
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="edxapp-config",
                                    mount_path="/openedx/config",
                                ),
                            ],
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="lms-edxapp",
                            image=cached_image_uri(
                                f"mitodl/edxapp@{EDXAPP_DOCKER_IMAGE_DIGEST}"
                            ),
                            command=["python"],
                            args=["process_scheduled_emails.py"],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SERVICE_VARIANT", value="lms"
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DJANGO_SETTINGS_MODULE",
                                    value="lms.envs.production",
                                ),
                            ],
                            volume_mounts=celery_volume_mounts,
                        ),
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=[
                *[v for v in lms_edxapp_config_sources.values() if v is not None],
                lms_app,
            ]
        ),
    )

    cms_celery_selector_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-cms-celery",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    cms_celery_labels = cms_celery_selector_labels | {
        "ol.mit.edu/edxapp-celery-sg": "true",
    }
    cms_celery_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-cms-celery-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{cms_celery_deployment_name}",
            namespace=namespace,
            labels=cms_celery_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=cms_celery_selector_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=cms_celery_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    security_context=pod_security_context,
                    volumes=cms_edxapp_volumes,
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="export-course-repos-mkdir",
                            image=cached_image_uri("busybox:1.35"),
                            command=[
                                "/bin/sh",
                                "-c",
                                "mkdir -p /openedx/data/export_course_repos",
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-data",
                                    mount_path="/openedx/data",
                                ),
                            ],
                        ),
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image=cached_image_uri("busybox:1.35"),
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/cms.env.yml"
                            ],
                            volume_mounts=[
                                *cms_edxapp_init_volume_mounts,
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="edxapp-config",
                                    mount_path="/openedx/config",
                                ),
                            ],
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="cms-edxapp",
                            image=cached_image_uri(
                                f"mitodl/edxapp@{EDXAPP_DOCKER_IMAGE_DIGEST}"
                            ),
                            command=["celery"],
                            args=[
                                "--app=cms.celery",
                                "worker",
                                "-B",
                                "-E",
                                "--loglevel=info",
                                "--hostname=edx.cms.core.default.%h",
                                "--max-tasks-per-child",
                                "100",
                                "--queues=edx.cms.core.default",
                                "--exclude-queues=edx.lms.core.default,edx.lms.core.high,edx.lms.core.high_mem",
                                "--prefetch-multiplier=1",
                                "--concurrency=2",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SERVICE_VARIANT", value="cms"
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DJANGO_SETTINGS_MODULE",
                                    value="cms.envs.production",
                                ),
                                *celery_env_vars,
                            ],
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={
                                    "cpu": resources_dict["celery"]["cms"][
                                        "cpu_request"
                                    ],
                                    "memory": resources_dict["celery"]["cms"][
                                        "memory_request"
                                    ],
                                },
                                limits={
                                    "memory": resources_dict["celery"]["cms"][
                                        "memory_limit"
                                    ],
                                },
                            ),
                            volume_mounts=celery_volume_mounts,
                        )
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=[v for v in cms_edxapp_config_sources.values() if v is not None]
        ),
    )

    # Create celery autoscaling resources (ScaledObjects for Redis-based scaling).
    # Webapp ScaledObjects are managed by the OLApplicationK8s component instances above.
    _autoscaling_resources = create_celery_autoscaling_resources(
        edxapp_cache=edxapp_cache,
        replicas_dict=replicas_dict,
        namespace=namespace,
        lms_celery_labels=lms_celery_labels,
        cms_celery_labels=cms_celery_labels,
        lms_celery_deployment_name=lms_celery_deployment_name,
        cms_celery_deployment_name=cms_celery_deployment_name,
        stack_info=stack_info,
        lms_celery_deployment=lms_celery_deployment,
        cms_celery_deployment=cms_celery_deployment,
    )

    # Meilisearch
    _meilisearch_helm_release = create_meilisearch_resources(
        stack_info=stack_info,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
    )

    # APISIX ingress configuration and setup
    apisix_ingress_class = edxapp_config.get("apisix_ingress_class") or "apisix"
    tls_secret_name = (
        "shared-backend-tls-pair"  # pragma: allowlist secret  # noqa: S105
    )
    cert_manager_certificate = OLCertManagerCert(
        f"ol-{stack_info.env_prefix}-edxapp-tls-cert-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="edxapp",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            create_apisixtls_resource=True,
            dest_secret_name=tls_secret_name,
            dns_names=[
                edxapp_config.require("backend_lms_domain"),
                edxapp_config.require("backend_studio_domain"),
                edxapp_config.require("backend_preview_domain"),
            ],
        ),
    )

    edxapp_shared_plugins = OLApisixSharedPlugins(
        f"ol-{stack_info.env_prefix}-edxapp-shared-plugins-{stack_info.env_suffix}",
        plugin_config=OLApisixSharedPluginsConfig(
            application_name="edxapp",
            resource_suffix="ol-shared-plugins",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            enable_defaults=True,
        ),
    )

    lms_apisixroute = OLApisixRoute(
        name=f"ol-{stack_info.env_prefix}-edxapp-lms-apisix-route-{stack_info.env_suffix}",
        k8s_namespace=namespace,
        k8s_labels=k8s_global_labels,
        route_configs=[
            OLApisixRouteConfig(
                route_name="lms-default",
                priority=0,
                plugins=[],
                shared_plugin_config_name=edxapp_shared_plugins.resource_name,
                hosts=[
                    edxapp_config.require("backend_lms_domain"),
                    edxapp_config.require_object("domains")["lms"],
                ],
                paths=["/*"],
                timeout_connect="600s",
                timeout_read="600s",
                timeout_send="600s",
                backend_service_name=lms_webapp_deployment_name,
                backend_service_port="http",
            ),
        ],
    )

    cms_apisixroute = OLApisixRoute(
        name=f"ol-{stack_info.env_prefix}-edxapp-cms-apisix-route-{stack_info.env_suffix}",
        k8s_namespace=namespace,
        k8s_labels=k8s_global_labels,
        route_configs=[
            OLApisixRouteConfig(
                route_name="cms-default",
                priority=0,
                plugins=[],
                shared_plugin_config_name=edxapp_shared_plugins.resource_name,
                hosts=[
                    edxapp_config.require("backend_studio_domain"),
                    edxapp_config.require_object("domains")["studio"],
                ],
                paths=["/*"],
                backend_service_name=cms_webapp_deployment_name,
                backend_service_port="http",
            ),
        ],
    )

    return {
        "edxapp_k8s_app_security_group_id": edxapp_k8s_app_security_group.id,
    }
