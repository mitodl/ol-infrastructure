import os

import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions, StackReference

from bridge.lib.magic_numbers import DEFAULT_NEXTJS_PORT
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import BusinessUnit, K8sGlobalLabels, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
# Assume the application image URI comes from a separate image build stack
app_image = "mitodl/mit-learn-nextjs-app"
if "MIT_LEARN_NEXTJS_DOCKER_TAG" not in os.environ:
    msg = "MIT_LEARN_NEXTJS_DOCKER_TAG environment varibale must be set."
    raise OSError(msg)
MIT_LEARN_NEXTJS_DOCKER_TAG = os.environ["MIT_LEARN_NEXTJS_DOCKER_TAG"]

k8s_global_labels = K8sGlobalLabels(
    service=Services.mit_learn, ou=BusinessUnit.mit_learn, stack=stack_info
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

learn_namespace = "mitlearn"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(learn_namespace, ns)
)

raw_env_vars = {
    "NEXT_PUBLIC_APPZI_URL": "sample",
    "NEXT_PUBLIC_CSRF_COOKIE_NAME": "sample",
    "NEXT_PUBLIC_EMBEDLY_KEY": "sample",
    "NEXT_PUBLIC_LEARN_AI_RECOMMENDATION_ENDPOINT": "https://api.rc.learn.mit.edu/sample",
    "NEXT_PUBLIC_LEARN_AI_SYLLABUS_ENDPOINT": "https://api.rc.learn.mit.edu/sample",
    "NEXT_PUBLIC_MITOL_API_BASE_URL": "https://api.rc.learn.mit.edu",
    "NEXT_PUBLIC_MITOL_AXIOS_WITH_CREDENTIALS": "true",
    "NEXT_PUBLIC_MITOL_SUPPORT_EMAIL": "mitlearn-support@mit.edu",
    "NEXT_PUBLIC_ORIGIN": "https://rc.learn.mit.edu",
    "NEXT_PUBLIC_POSTHOG_API_HOST": "https://ph.ol.mit.edu",
    "NEXT_PUBLIC_POSTHOG_API_KEY": "sample",  # pragma: allowlist secret
    "NEXT_PUBLIC_POSTHOG_PROJECT_ID": "sample",
    "NEXT_PUBLIC_SENTRY_DSN": "sample",
    "NEXT_PUBLIC_SENTRY_ENV": "sample",
    "NEXT_PUBLIC_SENTRY_PROFILES_SAMPLE_RATE": "sample",
    "NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE": "sample",
    "NEXT_PUBLIC_SITE_NAME": "MIT Learn",
    "NEXT_PUBLIC_VERSION": "sample",
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
        name="mit-learn-nextjs",
        namespace=learn_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=application_labels,
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="mit-learn-nextjs",
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
