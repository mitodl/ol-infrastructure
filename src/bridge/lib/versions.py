# renovate: datasource=github-releases depName=concourse packageName=concourse/concourse
CONCOURSE_VERSION = "8.1.1"  # (TMM 2026-03-06) - Pin to <8.1.0 because of some login bugs with stale state tokens
# renovate: datasource=github-releases depName=consul-template packageName=hashicorp/consul-template
CONSUL_TEMPLATE_VERSION = "0.41.4"
# renovate: datasource=github-releases depName=consul packageName=hashicorp/consul
CONSUL_VERSION = "1.22.6"
# renovate: datasource=pypi depName=jupyterhub packageName=jupyterhub
JUPYTERHUB_VERSION = "5.4.4"
# renovate: datasource=github-releases depName=keycloak packageName=keycloak/keycloak
KEYCLOAK_VERSION = "26.5.6"
# renovate: datasource=docker depName=leek packageName=kodhive/leek
LEEK_VERSION = "0.7.7"
# renovate: datasource=helm depName=open-metadata packageName=openmetadata registryUrl=https://helm.open-metadata.org
OPEN_METADATA_VERSION = "1.12.3"
OVS_VERSION = "v0.65.1-3-g2630021"
REDASH_VERSION = "9d273e4"
# renovate: datasource=github-releases depName=traefik packageName=traefik/traefik
TRAEFIK_VERSION = "3.6.12"
TUTOR_PERMISSIONS_VERSION = "15.3.4"
# renovate: datasource=github-releases depName=vault packageName=hashicorp/vault
VAULT_VERSION = "1.21.4"
# renovate: datasource=docker depName=vector packageName=timberio/vector
VECTOR_VERSION = "0.40.1"

# EKS Specific Versions
# renovate: datasource=helm depName=airbyte packageName=airbyte registryUrl=https://airbytehq.github.io/charts
AIRBYTE_CHART_VERSION = "2.0.19"
# renovate: datasource=helm depName=superset packageName=superset registryUrl=https://apache.github.io/superset
SUPERSET_CHART_VERSION = "0.15.2"
# renovate: datasource=helm depName=tika packageName=tika registryUrl=https://apache.jfrog.io/artifactory/tika
TIKA_CHART_VERSION = "3.2.2"
# renovate: datasource=helm depName=apisix packageName=apisix registryUrl=https://apache.github.io/apisix-helm-chart
APISIX_CHART_VERSION = "2.13.0"
# renovate: datasource=helm depName=aws-load-balancer-controller packageName=aws-load-balancer-controller registryUrl=https://aws.github.io/eks-charts
AWS_LOAD_BALANCER_CONTROLLER_CHART_VERSION = "3.1.0"
# renovate: datasource=helm depName=aws-node-termination-handler packageName=aws-node-termination-handler registryUrl=https://aws.github.io/eks-charts
AWS_NODE_TERMINATION_HANDLER_CHART_VERSION = "0.27.2"
# renovate: datasource=helm depName=cert-manager packageName=cert-manager
CERT_MANAGER_CHART_VERSION = "v1.16.1"
# renovate: datasource=helm depName=dagster packageName=dagster registryUrl=https://dagster-io.github.io/helm
DAGSTER_CHART_VERSION = "1.12.20"
# renovate: datasource=aws-eks-addon depName=ebs-csi-driver
EBS_CSI_DRIVER_VERSION = "v1.40.1-eksbuild.1"
# renovate: datasource=aws-eks-addon depName=efs-csi-driver
EFS_CSI_DRIVER_VERSION = "v2.1.6-eksbuild.1"
# renovate: datasource=helm depName=external-dns packageName=external-dns registryUrl=https://kubernetes-sigs.github.io/external-dns/
EXTERNAL_DNS_CHART_VERSION = "1.20.0"
# renovate: datasource=github-releases depName=gateway-api packageName=kubernetes-sigs/gateway-api
GATEWAY_API_VERSION = "v1.5.1"
# renovate: datasource=docker depName=karpenter packageName=public.ecr.aws/karpenter/karpenter
KARPENTER_CHART_VERSION = "1.10.0"
# renoavate: datasource=helm depName=keda registryUrl=https://kedacore.github.io/charts packageName=keda
KEDA_CHART_VERSION = "2.17.1"
# renovate: datasource=docker depName=kube-state-metrics packageName=registry-1.docker.io/bitnamicharts/kube-state-metrics
KUBE_STATE_METRICS_CHART_VERSION = "5.1.0"
# renovate: datasource=helm depName=kubewatch packageName=kubewatch registryUrl=https://robusta-charts.storage.googleapis.com
KUBEWATCH_CHART_VERSION = "3.5.0"
# renovate: datasource=helm depName=meilisearch packageName=meilisearch-kubernetes registryUrl=https://meilisearch.github.io/meilisearch-kubernetes
MEILISEARCH_CHART_VERSION = "0.23.0"
# renovate: datasource=helm depName=operator packageName=operator registryUrl=https://starrocks.github.io/starrocks-kubernetes-operator
STARROCKS_OPERATOR_CHART_VERSION = "1.11.4"
# renovate: datasource=helm depName=operator packageName=starrocks registryUrl=https://starrocks.github.io/starrocks-kubernetes-operator
STARROCKS_CHART_VERSION = "1.11.4"
# Altinity ClickHouse Operator — released as tag "release-X.Y.Z" on GitHub
# renovate: datasource=github-releases depName=clickhouse-operator packageName=Altinity/clickhouse-operator
CLICKHOUSE_OPERATOR_VERSION = "release-0.26.0"
# renovate: datasource=docker depName=altinity/clickhouse-server packageName=altinity/clickhouse-server
CLICKHOUSE_SERVER_VERSION = "25.8.1.2953.altinitystable"
# renovate: datasource=docker depName=clickhouse/clickhouse-keeper packageName=clickhouse/clickhouse-keeper
CLICKHOUSE_KEEPER_VERSION = "26.2-alpine"
# renovate: datasource=helm depName=traefik packageName=traefik registryUrl=https://traefik.github.io/charts
TRAEFIK_CHART_VERSION = "39.0.6"
# renovate: datasource=helm depName=vantage-kubernetes-agent packageName=vantage-kubernetes-agent registryUrl=https://vantage-sh.github.io/helm-charts
VANTAGE_K8S_AGENT_CHART_VERSION = "1.4.0"
# renovate: datasource=helm depName=vault-secrets-operator packageName=vault-secrets-operator registryUrl=https://helm.releases.hashicorp.com
VAULT_SECRETS_OPERATOR_CHART_VERSION = "1.3.0"
# renovate: datasource=docker depName=nginx
NGINX_VERSION = "1.29.6"
# renovate: datasource=github-releases depName=prometheus-operator packageName=prometheus-operator/prometheus-operator
PROMETHEUS_OPERATOR_CRD_VERSION = "v0.90.1"
# renovate: datasource=github-releases depName=keycloak-k8s-resources packageName=keycloak/keycloak-k8s-resources
KEYCLOAK_OPERATOR_CRD_VERSION = "26.2.4"
# renovate: datasource=helm depName=jupyterhub packageName=jupyterhub registryUrl=https://hub.jupyter.org/helm-chart
JUPYTERHUB_CHART_VERSION = "4.3.3"
# renovate: datasource=helm depName=k8s-monitoring packageName=k8s-monitoring registryUrl=https://grafana.github.io/helm-charts
GRAFANA_K8S_MONITORING_CHART_VERSION = "3.8.4"
# renovate: datasource=helm depName=dcgm-exporter packageName=dcgm-exporter registryUrl=https://nvidia.github.io/dcgm-exporter/helm-charts
NVIDIA_DCGM_EXPORTER_CHART_VERSION = "4.8.1"
# renovate: datasource=helm depName=nvidia-device-plugin packageName=nvidia-device-plugin registryUrl=https://nvidia.github.io/k8s-device-plugin
NVIDIA_K8S_DEVICE_PLUGIN_CHART_VERSION = "0.19.0"
# renovate: datasource=docker depName=pgbouncer packageName=ghcr.io/cloudnative-pg/pgbouncer
PGBOUNCER_VERSION = "1.25.1"
# renovate: datasource=helm depName=local-path-provisioner packageName=local-path-provisioner registryUrl=https://charts.rancher.io
LOCAL_PATH_PROVISIONER_CHART_VERSION = "0.0.31"
# renovate: datasource=github-releases depName=qdrant packageName=qdrant/qdrant
QDRANT_VERSION = "v1.17.0"
