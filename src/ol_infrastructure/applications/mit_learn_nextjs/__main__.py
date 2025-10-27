"""Pulumi program for deploying the MIT Learn Next.js application to Kubernetes."""

import os

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.magic_numbers import DEFAULT_NEXTJS_PORT
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import BusinessUnit, K8sGlobalLabels, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
# Assume the application image URI comes from a separate image build stack
if "MIT_LEARN_NEXTJS_DOCKER_TAG" not in os.environ:
    msg = "MIT_LEARN_NEXTJS_DOCKER_TAG environment varibale must be set."
    raise OSError(msg)
MIT_LEARN_NEXTJS_DOCKER_TAG = os.environ["MIT_LEARN_NEXTJS_DOCKER_TAG"]

app_image = cached_image_uri(
    f"mitodl/mit-learn-nextjs-app:{MIT_LEARN_NEXTJS_DOCKER_TAG}"
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.mit_learn, ou=BusinessUnit.mit_learn, stack=stack_info
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

learn_namespace = "mitlearn"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(learn_namespace, ns)
)

nextjs_config = Config("nextjs")

raw_env_vars = {
    # Env vars available only on server
    "MITOL_NOINDEX": nextjs_config.get("mitol_noindex"),
    "OPTIMIZE_IMAGES": nextjs_config.get("optimize_images"),
    # Env vars available on client and server
    "NEXT_PUBLIC_APPZI_URL": nextjs_config.require("appzi_url"),
    "NEXT_PUBLIC_CSRF_COOKIE_NAME": nextjs_config.require("csrf_cookie_name"),
    "NEXT_PUBLIC_EMBEDLY_KEY": nextjs_config.require("embedly_key"),
    "NEXT_PUBLIC_LEARN_AI_RECOMMENDATION_ENDPOINT": nextjs_config.require(
        "recommendation_endpoint"
    ),
    "NEXT_PUBLIC_LEARN_AI_SYLLABUS_ENDPOINT": nextjs_config.require(
        "syllabus_endpoint"
    ),
    "NEXT_PUBLIC_MITOL_API_BASE_URL": nextjs_config.require("mitlearn_api_base_url"),
    "NEXT_PUBLIC_MITX_ONLINE_CSRF_COOKIE_NAME": "csrf_mitxonline",
    "NEXT_PUBLIC_MITX_ONLINE_BASE_URL": nextjs_config.require("mitxonline_base_url"),
    "NEXT_PUBLIC_MITOL_AXIOS_WITH_CREDENTIALS": "true",
    "NEXT_PUBLIC_MITOL_SUPPORT_EMAIL": "mitlearn-support@mit.edu",
    "NEXT_PUBLIC_ORIGIN": nextjs_config.require("origin"),
    "NEXT_PUBLIC_POSTHOG_API_HOST": nextjs_config.require("posthog_api_host"),
    "NEXT_PUBLIC_POSTHOG_API_KEY": nextjs_config.require("posthog_api_key"),
    "NEXT_PUBLIC_POSTHOG_PROJECT_ID": nextjs_config.require("posthog_project_id"),
    "NEXT_PUBLIC_SENTRY_DSN": nextjs_config.require("sentry_dsn"),
    "NEXT_PUBLIC_SENTRY_ENV": nextjs_config.require("sentry_env"),
    "NEXT_PUBLIC_SENTRY_PROFILES_SAMPLE_RATE": "0.25",
    "NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE": "0.25",
    "NEXT_PUBLIC_SITE_NAME": "MIT Learn",
    "NEXT_PUBLIC_VERSION": MIT_LEARN_NEXTJS_DOCKER_TAG,
}

env_vars = []
for k, v in raw_env_vars.items():
    env_vars.append(
        kubernetes.core.v1.EnvVarArgs(
            name=k,
            value=v,
        )
    )

application_labels = k8s_global_labels | {
    "ol.mit.edu/service": "nextjs",
}

# Define the Persistent Volume Claim for shared build cache on EFS
nextjs_pvc_name = "nextjs-build-cache-efs"
nextjs_build_cache_pvc = kubernetes.core.v1.PersistentVolumeClaim(
    f"mit-learn-nextjs-{stack_info.name}-pvc",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=nextjs_pvc_name,
        namespace=learn_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.core.v1.PersistentVolumeClaimSpecArgs(
        access_modes=["ReadWriteMany"],
        resources=kubernetes.core.v1.VolumeResourceRequirementsArgs(
            requests={"storage": "10Gi"},
        ),
        storage_class_name="efs-sc",  # Assumes 'efs-sc' StorageClass is configured
    ),
)

# Define the volume to be used by the deployment and job
efs_volume = kubernetes.core.v1.VolumeArgs(
    name=nextjs_pvc_name,
    persistent_volume_claim=kubernetes.core.v1.PersistentVolumeClaimVolumeSourceArgs(
        claim_name=nextjs_pvc_name,
    ),
)

# Define the volume mount for the EFS volume
efs_volume_mount = kubernetes.core.v1.VolumeMountArgs(
    name=nextjs_pvc_name,
    mount_path="/app/frontends/main/.next",
)

# Kubernetes Job to build the NextJS application and store output on EFS
mit_learn_nextjs_build_job = kubernetes.batch.v1.Job(
    f"mit-learn-nextjs-{stack_info.name}-build-job",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mitlearn-nextjs-build",
        namespace=learn_namespace,
        labels=k8s_global_labels
        | {
            "ol.mit.edu/job": "nextjs_build",
        },
    ),
    spec=kubernetes.batch.v1.JobSpecArgs(
        active_deadline_seconds=600,  # 10 minute timeout
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=k8s_global_labels
                | {
                    "ol.mit.edu/job": "nextjs_build",
                },
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                restart_policy="Never",
                volumes=[efs_volume],
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="nextjs-builder-job",
                        image=app_image,
                        command=["yarn", "build", "--no-lint"],
                        volume_mounts=[efs_volume_mount],
                        image_pull_policy="Always",
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            limits={"memory": "8Gi"},
                        ),
                        env=env_vars,
                    ),
                ],
            ),
        ),
        backoff_limit=3,  # Allow retries for transient failures
    ),
    opts=ResourceOptions(parent=nextjs_build_cache_pvc),
)

mit_learn_nextjs_deployment = kubernetes.apps.v1.Deployment(
    f"mit-learn-nextjs-{stack_info.name}-deployment-resource",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mitlearn-nextjs",
        namespace=learn_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=application_labels,
        ),
        replicas=nextjs_config.get_int("pod_count") or 2,
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="mitlearn-nextjs",
                namespace=learn_namespace,
                labels=application_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                volumes=[
                    efs_volume,  # Use the EFS volume
                ],
                # init_containers removed, build is now handled by a separate Job
                dns_policy="ClusterFirst",
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="nextjs-app",
                        image=app_image,
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                container_port=DEFAULT_NEXTJS_PORT,
                                name="http",
                            )
                        ],
                        image_pull_policy="Always",
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(),
                        env=env_vars,
                        volume_mounts=[efs_volume_mount],  # Mount EFS volume
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=DEFAULT_NEXTJS_PORT,
                            ),
                            initial_delay_seconds=30,  # Wait 30 seconds before first
                            # probe
                            period_seconds=30,
                            failure_threshold=3,  # Consider failed after 3 attempts
                        ),
                        # Readiness probe to check if the application is ready to serve
                        # traffic
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=DEFAULT_NEXTJS_PORT,
                            ),
                            initial_delay_seconds=15,  # Wait 15 seconds before first
                            # probe
                            period_seconds=15,
                            failure_threshold=3,  # Consider failed after 3 attempts
                        ),
                        # Startup probe to ensure the application is fully initialized
                        # before other probes start
                        startup_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=DEFAULT_NEXTJS_PORT,
                            ),
                            initial_delay_seconds=10,  # Wait 10 seconds before first
                            # probe
                            period_seconds=10,  # Probe every 10 seconds
                            failure_threshold=30,  # Allow up to 5 minutes (30 * 10s)
                            # for startup
                            success_threshold=1,
                            timeout_seconds=5,
                        ),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[mit_learn_nextjs_build_job],  # Ensure build job runs first
    ),
)

# Create a Kubernetes Service to expose the deployment
mit_learn_nextjs_service_name = "mit-learn-nextjs"
mit_learn_nextjs_service = kubernetes.core.v1.Service(
    f"mit-learn-nextjs-{stack_info.name}-service-resource",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mit-learn-nextjs",
        namespace=learn_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        selector=application_labels,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                port=DEFAULT_NEXTJS_PORT,
                target_port=DEFAULT_NEXTJS_PORT,
                protocol="TCP",
                name="http",
            )
        ],
        type="ClusterIP",
    ),
    opts=ResourceOptions(
        parent=mit_learn_nextjs_deployment,
    ),
)

gateway = OLEKSGateway(
    f"mit-learn-nextjs-{stack_info.name}-gateway",
    gateway_config=OLEKSGatewayConfig(
        cert_issuer="letsencrypt-production",
        cert_issuer_class="cluster-issuer",
        gateway_name="mit-learn-nextjs-gateway",
        labels=k8s_global_labels,
        namespace=learn_namespace,
        listeners=[
            OLEKSGatewayListenerConfig(
                name="https",
                hostname=nextjs_config.require("domain"),
                port=8443,
                tls_mode="Terminate",
                certificate_secret_name="mit-learn-nextjs-tls",  # noqa: S106  # pragma: allowlist secret
                certificate_secret_namespace=learn_namespace,
            ),
        ],
        routes=[
            OLEKSGatewayRouteConfig(
                backend_service_name=mit_learn_nextjs_service_name,
                backend_service_namespace=learn_namespace,
                backend_service_port=DEFAULT_NEXTJS_PORT,
                hostnames=[nextjs_config.require("domain")],
                name="mit-learn-nextjs-https",
                listener_name="https",
                port=8443,
            ),
        ],
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)
