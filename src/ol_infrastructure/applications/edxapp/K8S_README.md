# What is going on here?

## EDXAPP Deployment into K8S using Pulumi

This directory contains the code to deploy the edX platform into a Kubernetes cluster using Pulumi. The deployment is designed to be modular and configurable, allowing for easy customization and scaling.

## Files and Resources

- `k8s_configmaps.py` - creates all the configmaps needed for edxapp. Anything not sentisitive goes here.
  - Static configs can go into these files as appropriate:
    - `files/edxapp/<app>/50-general-config.yaml`
    - `files/edxapp/<app>/71-cms-general-config.yaml`
    - `files/edxapp/<app>/81-lms-general-config.yaml`
  - Configuration that requires interpolation and/or dynamic environment specific values should be added to the appropriate `interpolated` block within `k8s_configmaps.py`.
- `k8s_secrets.py` - Creates all the secrets needed for edxapp. Anything sensitive in nature goes here. This is primarilly accomplished through the use of `VaultDynamicSecret` and `VaultStaticSecret` resources following our general pattern for getting secrets out of vault and into K8S.
- `k8s_resources.py` - creates all the K8S resources needed for edxapp. This includes Deployments, Services, HPAs, ScaledObjects.
  - A hybrid `OLEKSTrustRole` which uses the vault-service account for the application and is also allowed to interact with the AWS API.
  - A security group for the application pods to allow them to interact with AWS services / rds / redis.
  - A persistent volume claim for `/openedx/data` to share course import an export files.
    - This eliminates the need for sticky sessions.
  - A `config-aggregator` container definition. This container will mount all the configmaps and secrets and aggregate them into a single location for consumption by edxapp.
  - Deployments and pre-deployment jobs for CMS.
    - pre-deployment `job` to run migrations.
    - Webapp `deployment` with a `hpa` hooked to memory and CPU for autoscaling.
    - A webapp service definition to expose the webapp internally within the cluster and to the ingress resources.
    - Celery `deployment` with a `scaledobject` hooked to redis for autoscaling.
  - Deployments and pre-deployment jobs for LMS.
    - pre-deployment `job` to run migrations.
    - pre-deployment `job` to run the waffle-flag sync script.
    - Webapp `deployment` with a `hpa` hooked to memory and CPU for autoscaling.
    - A webapp service definition to expose the webapp internally within the cluster and to the ingress resources.
    - Celery `deployment` with a `scaledobject` hooked to redis for autoscaling.
    - A process-scheduled-emails `deployment` which invoked our custom script for sending out scheduled emails every x minutes. This is pretty hacky and should be a celery job but alas.
- `k8s_ingress_resources.py` - This creates all the ingress / traefik resources needed for exposing edxapp to the internet. We use our custom `OLEKSGateway` resource to create these resources.


## A note about deployment labels

Each deployment should have at least one unique label (generally `ol.mit.edu/component`). This will ensure that any `Services` only point to their intended targets. Like, for instance, if the webapp and celery deployments shared a fullset of labels, then the webapp 'Service' would randomly route traffic to the `celery` pods, which are not expecting any web traffic (and in fact are not even listening on any port!). That would be bad and has bitten us in the past.

## A note about "pre-deployment" jobs

We are utilizing a somewhat janky method via pulumi to execute a kubernetes `Job` prior to updating any `Deployment` resources. The jobs have a TTL of 30 minutes, which means if the stack is `up`'d multiple times in a short period, the job will only run once. This is generally what we want, as we don't want to run migrations or the waffle-flag sync script multiple times in a row. However it might not be what you want if, say, the migrations failed... If this is the case, you will want to manually delete the `Job` resource with `kubectl` or your preferred k8s management tool.

## A note about configuration aggregation
We are using a custom `config-aggregator` container to aggregate all the configmaps and secrets into a single location for consumption by edxapp. This is done to simplify the configuration management and to ensure that all configuration is available to edxapp at runtime. The `config-aggregator` container mounts all the configmaps and secrets and aggregates them into `/openedx/config/<cms|lms>.env.yaml`. The actual container that runs edxapp will only see the fully rendered `/openedx/config/<cms|lms>.env.yaml` file and nothing else.

## A note about configmap and secret naming

All the configmaps and secrets are named with `<some number>-<purpose>.yaml` to control the ordering when the config aggregator runs the concatenation process. This is important because some configuration files need to be loaded before others. For example, the `00-database-crednetials.yaml` file needs to be loaded before any other file because it contains anchors referenced in other parts of the configuration.
