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

app_image = f"mitodl/mit-learn-nextjs-app:{MIT_LEARN_NEXTJS_DOCKER_TAG}"

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
    "NEXT_PUBLIC_MITX_ONLINE_BASE_URL": nextjs_config.require("mitxonline_base_url"),
    "NEXT_PUBLIC_MITOL_AXIOS_WITH_CREDENTIALS": "true",
    "NEXT_PUBLIC_MITOL_SUPPORT_EMAIL": "mitlearn-support@mit.edu",
    "NEXT_PUBLIC_ORIGIN": nextjs_config.require("origin"),
    "NEXT_PUBLIC_POSTHOG_API_HOST": nextjs_config.require("posthog_api_host"),
    "NEXT_PUBLIC_POSTHOG_API_KEY": nextjs_config.require("posthog_api_key"),
    "NEXT_PUBLIC_POSTHOG_PROJECT_ID": nextjs_config.require("posthog_project_id"),
    "NEXT_PUBLIC_SENTRY_DSN": nextjs_config.require("sentry_dsn"),
    "NEXT_PUBLIC_SENTRY_ENV": nextjs_config.require("sentry_env"),
    "NEXT_PUBLIC_SENTRY_PROFILES_SAMPLE_RATE": "1",
    "NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE": "1",
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

# Define the volume for shared build cache
nextjs_build_cache_volume = kubernetes.core.v1.VolumeArgs(
    name="nextjs-build-cache",
    empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
)

# Define the volume mount for the shared build cache
nextjs_build_cache_volume_mount = kubernetes.core.v1.VolumeMountArgs(
    name="nextjs-build-cache",
    mount_path="/app/frontends/main/.next",
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
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="mitlearn-nextjs",
                namespace=learn_namespace,
                labels=application_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                volumes=[
                    nextjs_build_cache_volume,
                ],
                init_containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="nextjs-builder",
                        image=app_image,
                        command=["yarn", "build"],
                        volume_mounts=[nextjs_build_cache_volume_mount],
                        image_pull_policy="Always",
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(),
                        env=env_vars,
                    ),
                ],
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
                        volume_mounts=[nextjs_build_cache_volume_mount],
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/healthcheck/",
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
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/healthcheck/",
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
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/healthcheck/",
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
