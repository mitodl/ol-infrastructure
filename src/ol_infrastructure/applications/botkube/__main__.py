# ruff: noqa: CPY001, D100
import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import BOTKUBE_CHART_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    Environment,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()

botkube_config = Config("config_botkube")
vault_config = Config("vault")
opensearch_stack = StackReference(
    f"infrastructure.aws.opensearch.apps.{stack_info.name}"
)
# opensearch_cluster = opensearch_stack.require_output("cluster")  noqa: ERA001
# opensearch_cluster_endpoint = opensearch_cluster["endpoint"]  noqa: ERA001

cluster_stack = StackReference(f"infrastructure.aws.eks.operations.{stack_info.name}")

aws_config = AWSBase(
    tags={"OU": BusinessUnit.operations, "Environment": Environment.operations},
)

botkube_namespace = "operations"

k8s_global_labels = K8sGlobalLabels(
    service=Services.botkube,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()

# Begin vault hoo-ha.
botkube_vault_secrets = read_yaml_secrets(
    Path(f"botkube/secrets.{stack_info.env_suffix}.yaml"),
)

botkube_vault_mount = vault.Mount(
    f"botkube-secrets-mount-{stack_info.env_suffix}",
    path="secret-botkube",
    type="kv-v2",
    options={"version": "2"},
    description="Secrets for the learn ai application.",
    opts=ResourceOptions(delete_before_replace=True),
)

botkube_static_vault_secrets = vault.generic.Secret(
    f"botkube-secrets-{stack_info.env_suffix}",
    path=botkube_vault_mount.path.apply("{}/secrets".format),
    data_json=json.dumps(botkube_vault_secrets),
)

botkube_vault_policy = vault.Policy(
    f"botkube-vault-policy-{stack_info.env_suffix}",
    name="botkube",
    policy=Path(__file__).parent.joinpath("botkube_policy.hcl").read_text(),
)

botkube_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"botkube-vault-auth-backend-role-{stack_info.env_suffix}",
    role_name="botkube",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[botkube_namespace],
    token_policies=[botkube_vault_policy.name],
)

# Stopped at: Make a kubernetes auth backend role that uses the policy we just installed
vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="botkube",
    namespace=botkube_namespace,
    labels=k8s_global_labels,
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=botkube_vault_auth_backend_role.role_name,
)

vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[botkube_vault_auth_backend_role],
    ),
)

# Load the static secrets into a k8s secret via VSO
static_secrets_name = "communication-slack"  # pragma: allowlist secret
static_secrets = OLVaultK8SSecret(
    name=f"botkube-{stack_info.env_suffix}-static-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="botkube-static-secrets",
        namespace=botkube_namespace,
        labels=k8s_global_labels,
        dest_secret_name=static_secrets_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-botkube",
        mount_type="kv-v2",
        path="secrets",
        includes=["*"],
        excludes=[],
        exclude_raw=True,
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
        depends_on=[botkube_static_vault_secrets],
    ),
)
# end Vault hoo-ha


setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(botkube_namespace, ns)
)

# Install the botkube helm chart
botkube_application = kubernetes.helm.v3.Release(
    f"botkube-{stack_info.name}-application-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="botkube",
        chart="botkube",
        version=BOTKUBE_CHART_VERSION,
        namespace=botkube_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://charts.botkube.io",
        ),
        values={
            "commonLabels": k8s_global_labels,
            # Botkube Helm values converted to Python dictionary
            # Botkube image configuration
            "image": {
                "registry": "ghcr.io",
                "repository": "kubeshop/botkube",
                "pullPolicy": "IfNotPresent",
                "tag": "v1.14.0",
            },
            # Pod Security Policy configuration
            "podSecurityPolicy": {"enabled": False},
            # Security context configuration
            "securityContext": {"runAsUser": 101, "runAsGroup": 101},
            # Container security context
            "containerSecurityContext": {
                "privileged": False,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
            },
            # RBAC configuration
            "rbac": {
                "serviceAccountMountPath": "/var/run/7e7fd2f5-b15d-4803-bc52-f54fba357e76/secrets/kubernetes.io/serviceaccount",  # noqa: E501
                "create": True,
                "rules": [],  # Deprecated
                "staticGroupName": "",  # Deprecated
                "groups": {
                    "botkube-plugins-default": {
                        "create": True,
                        "rules": [
                            {
                                "apiGroups": ["*"],
                                "resources": ["*"],
                                "verbs": ["get", "watch", "list"],
                            }
                        ],
                    }
                },
            },
            # Kubeconfig settings
            "kubeconfig": {"enabled": False, "base64Config": "", "existingSecret": ""},
            # Actions configuration
            "actions": {
                "describe-created-resource": {
                    "enabled": False,
                    "displayName": "Describe created resource",
                    "command": "kubectl describe {{ .Event.Kind | lower }}{{ if .Event.Namespace }} -n {{ .Event.Namespace }}{{ end }} {{ .Event.Name }}",  # noqa: E501
                    "bindings": {
                        "sources": ["k8s-create-events"],
                        "executors": ["k8s-default-tools"],
                    },
                },
                "show-logs-on-error": {
                    "enabled": False,
                    "displayName": "Show logs on error",
                    "command": "kubectl logs {{ .Event.Kind | lower }}/{{ .Event.Name }} -n {{ .Event.Namespace }}",  # noqa: E501
                    "bindings": {
                        "sources": ["k8s-err-with-logs-events"],
                        "executors": ["k8s-default-tools"],
                    },
                },
            },
            # Sources configuration
            "sources": {
                "k8s-recommendation-events": {
                    "displayName": "Kubernetes Recommendations",
                    "botkube/kubernetes": {
                        "context": {
                            "rbac": {
                                "group": {
                                    "type": "Static",
                                    "prefix": "",
                                    "static": {"values": ["botkube-plugins-default"]},
                                }
                            }
                        },
                        "enabled": True,
                        "config": {
                            "namespaces": {"include": [".*"]},
                            "recommendations": {
                                "pod": {"noLatestImageTag": True, "labelsSet": True},
                                "ingress": {
                                    "backendServiceValid": True,
                                    "tlsSecretValid": True,
                                },
                            },
                        },
                    },
                },
                "k8s-all-events": {
                    "displayName": "Kubernetes Info",
                    "botkube/kubernetes": {
                        "context": {
                            "rbac": {
                                "group": {
                                    "type": "Static",
                                    "prefix": "",
                                    "static": {"values": ["botkube-plugins-default"]},
                                }
                            }
                        },
                        "enabled": True,
                        "config": {
                            "filters": {
                                "objectAnnotationChecker": True,
                                "nodeEventsChecker": True,
                            },
                            "namespaces": {"include": [".*"]},
                            "event": {
                                "types": ["create", "delete", "error"],
                                "reason": {"include": [], "exclude": []},
                                "message": {"include": [], "exclude": []},
                            },
                            "annotations": {},
                            "labels": {},
                            "resources": [
                                {"type": "v1/pods"},
                                {"type": "v1/services"},
                                {"type": "networking.k8s.io/v1/ingresses"},
                                {
                                    "type": "v1/nodes",
                                    "event": {
                                        "message": {
                                            "exclude": [".*nf_conntrack_buckets.*"]
                                        }
                                    },
                                },
                                {"type": "v1/namespaces"},
                                {"type": "v1/persistentvolumes"},
                                {"type": "v1/persistentvolumeclaims"},
                                {"type": "v1/configmaps"},
                                {"type": "rbac.authorization.k8s.io/v1/roles"},
                                {"type": "rbac.authorization.k8s.io/v1/rolebindings"},
                                {
                                    "type": "rbac.authorization.k8s.io/v1/clusterrolebindings"  # noqa: E501
                                },
                                {"type": "rbac.authorization.k8s.io/v1/clusterroles"},
                                {
                                    "type": "apps/v1/daemonsets",
                                    "event": {
                                        "types": ["create", "update", "delete", "error"]
                                    },
                                    "updateSetting": {
                                        "includeDiff": True,
                                        "fields": [
                                            "spec.template.spec.containers[*].image",
                                            "status.numberReady",
                                        ],
                                    },
                                },
                                {
                                    "type": "batch/v1/jobs",
                                    "event": {
                                        "types": ["create", "update", "delete", "error"]
                                    },
                                    "updateSetting": {
                                        "includeDiff": True,
                                        "fields": [
                                            "spec.template.spec.containers[*].image",
                                            "status.conditions[*].type",
                                        ],
                                    },
                                },
                                {
                                    "type": "apps/v1/deployments",
                                    "event": {
                                        "types": ["create", "update", "delete", "error"]
                                    },
                                    "updateSetting": {
                                        "includeDiff": True,
                                        "fields": [
                                            "spec.template.spec.containers[*].image",
                                            "status.availableReplicas",
                                        ],
                                    },
                                },
                                {
                                    "type": "apps/v1/statefulsets",
                                    "event": {
                                        "types": ["create", "update", "delete", "error"]
                                    },
                                    "updateSetting": {
                                        "includeDiff": True,
                                        "fields": [
                                            "spec.template.spec.containers[*].image",
                                            "status.readyReplicas",
                                        ],
                                    },
                                },
                            ],
                        },
                    },
                },
                "k8s-err-events": {
                    "displayName": "Kubernetes Errors",
                    "botkube/kubernetes": {
                        "context": {
                            "rbac": {
                                "group": {
                                    "type": "Static",
                                    "prefix": "",
                                    "static": {"values": ["botkube-plugins-default"]},
                                }
                            }
                        },
                        "enabled": True,
                        "config": {
                            "namespaces": {"include": [".*"]},
                            "event": {"types": ["error"]},
                            "resources": [
                                {"type": "v1/pods"},
                                {"type": "v1/services"},
                                {"type": "networking.k8s.io/v1/ingresses"},
                                {
                                    "type": "v1/nodes",
                                    "event": {
                                        "message": {
                                            "exclude": [".*nf_conntrack_buckets.*"]
                                        }
                                    },
                                },
                                {"type": "v1/namespaces"},
                                {"type": "v1/persistentvolumes"},
                                {"type": "v1/persistentvolumeclaims"},
                                {"type": "v1/configmaps"},
                                {"type": "rbac.authorization.k8s.io/v1/roles"},
                                {"type": "rbac.authorization.k8s.io/v1/rolebindings"},
                                {
                                    "type": "rbac.authorization.k8s.io/v1/clusterrolebindings"  # noqa: E501
                                },
                                {"type": "rbac.authorization.k8s.io/v1/clusterroles"},
                                {"type": "apps/v1/deployments"},
                                {"type": "apps/v1/statefulsets"},
                                {"type": "apps/v1/daemonsets"},
                                {"type": "batch/v1/jobs"},
                            ],
                        },
                    },
                },
                "k8s-err-with-logs-events": {
                    "displayName": "Kubernetes Errors for resources with logs",
                    "botkube/kubernetes": {
                        "context": {
                            "rbac": {
                                "group": {
                                    "type": "Static",
                                    "prefix": "",
                                    "static": {"values": ["botkube-plugins-default"]},
                                }
                            }
                        },
                        "enabled": True,
                        "config": {
                            "namespaces": {"include": [".*"]},
                            "event": {"types": ["error"]},
                            "resources": [
                                {"type": "v1/pods"},
                                {"type": "apps/v1/deployments"},
                                {"type": "apps/v1/statefulsets"},
                                {"type": "apps/v1/daemonsets"},
                                {"type": "batch/v1/jobs"},
                            ],
                        },
                    },
                },
                "k8s-create-events": {
                    "displayName": "Kubernetes Resource Created Events",
                    "botkube/kubernetes": {
                        "context": {
                            "rbac": {
                                "group": {
                                    "type": "Static",
                                    "prefix": "",
                                    "static": {"values": ["botkube-plugins-default"]},
                                }
                            }
                        },
                        "enabled": True,
                        "config": {
                            "namespaces": {"include": [".*"]},
                            "event": {"types": ["create"]},
                            "resources": [
                                {"type": "v1/pods"},
                                {"type": "v1/services"},
                                {"type": "networking.k8s.io/v1/ingresses"},
                                {"type": "v1/nodes"},
                                {"type": "v1/namespaces"},
                                {"type": "v1/configmaps"},
                                {"type": "apps/v1/deployments"},
                                {"type": "apps/v1/statefulsets"},
                                {"type": "apps/v1/daemonsets"},
                                {"type": "batch/v1/jobs"},
                            ],
                        },
                    },
                },
            },
            # Executors configuration
            "executors": {
                "k8s-default-tools": {
                    "botkube/kubectl": {
                        "displayName": "Kubectl",
                        "enabled": False,
                        "config": {"defaultNamespace": "default"},
                        "context": {
                            "rbac": {
                                "group": {
                                    "type": "Static",
                                    "prefix": "",
                                    "static": {"values": ["botkube-plugins-default"]},
                                }
                            }
                        },
                    },
                    "botkubeExtra/helm": {
                        "displayName": "Helm",
                        "enabled": True,
                        "context": {
                            "rbac": {
                                "group": {
                                    "type": "Static",
                                    "prefix": "",
                                    "static": {"values": ["botkube-plugins-default"]},
                                }
                            }
                        },
                    },
                }
            },
            # Command aliases
            "aliases": {
                "kc": {"command": "kubectl", "displayName": "Kubectl alias"},
                "k": {"command": "kubectl", "displayName": "Kubectl alias"},
            },
            # Communications secret
            "existingCommunicationsSecretName": "",
            # Communications configuration
            "communications": {
                "default-group": {
                    "socketSlack": {
                        "enabled": False,
                        "channels": {
                            "default": {
                                "name": "#botkube-ci",
                                "bindings": {
                                    "executors": ["k8s-default-tools"],
                                    "sources": [
                                        "k8s-err-events",
                                        "k8s-recommendation-events",
                                    ],
                                },
                            }
                        },
                        # These will get consumed from k8s secrets as env vars
                        # "botToken": "", noqa: ERA001
                        # "appToken": "", noqa: ERA001
                    },
                    "elasticsearch": {
                        "enabled": False,
                        "awsSigning": {
                            "enabled": False,
                            "awsRegion": "us-east-1",
                            "roleArn": "",
                        },
                        "server": "ELASTICSEARCH_ADDRESS",
                        "username": "ELASTICSEARCH_USERNAME",
                        "password": "ELASTICSEARCH_PASSWORD",
                        "skipTLSVerify": False,
                        "logLevel": "",
                        "indices": {
                            "default": {
                                "name": "botkube",
                                "type": "botkube-event",
                                "shards": 1,
                                "replicas": 0,
                                "bindings": {
                                    "sources": [
                                        "k8s-err-events",
                                        "k8s-recommendation-events",
                                    ]
                                },
                            }
                        },
                    },
                    "webhook": {
                        "enabled": False,
                        "url": "WEBHOOK_URL",
                        "bindings": {
                            "sources": ["k8s-err-events", "k8s-recommendation-events"]
                        },
                    },
                },
            },
            # Global settings
            "settings": {
                "clusterName": "not-configured",
                "healthPort": 2114,
                "upgradeNotifier": True,
                "log": {"level": "info", "disableColors": False, "formatter": "json"},
                "systemConfigMap": {"name": "botkube-system"},
                "persistentConfig": {
                    "startup": {
                        "configMap": {
                            "name": "botkube-startup-config",
                            "annotations": {},
                        },
                        "fileName": "_startup_state.yaml",
                    },
                    "runtime": {
                        "configMap": {
                            "name": "botkube-runtime-config",
                            "annotations": {},
                        },
                        "fileName": "_runtime_state.yaml",
                    },
                },
            },
            # SSL configuration
            "ssl": {"enabled": False, "existingSecretName": "", "cert": ""},
            # Service configuration
            "service": {"name": "metrics", "port": 2112, "targetPort": 2112},
            # ServiceMonitor configuration
            "serviceMonitor": {
                "enabled": False,
                "interval": "10s",
                "path": "/metrics",
                "port": "metrics",
                "labels": {},
            },
            # Deployment configuration
            "deployment": {
                "annotations": {},
                "livenessProbe": {
                    "initialDelaySeconds": 1,
                    "periodSeconds": 2,
                    "timeoutSeconds": 1,
                    "failureThreshold": 35,
                    "successThreshold": 1,
                },
                "readinessProbe": {
                    "initialDelaySeconds": 1,
                    "periodSeconds": 2,
                    "timeoutSeconds": 1,
                    "failureThreshold": 35,
                    "successThreshold": 1,
                },
            },
            # Pod configuration
            "replicaCount": 1,
            "extraAnnotations": {},
            "extraLabels": {},
            "priorityClassName": "",
            "nameOverride": "",
            "fullnameOverride": "",
            # Resource limits and requests
            "resources": {},
            # Environment variables
            "extraEnv": [
                {"name": "LOG_LEVEL_SOURCE_BOTKUBE_KUBERNETES", "value": "debug"},
                {
                    "name": "BOTKUBE_COMMUNICATIONS_DEFAULT-GROUP_SOCKET__SLACK_APP__TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "communication-slack",
                            "key": "slack-bot-token",
                        },
                    },
                },
                {
                    "name": "BOTKUBE_COMMUNICATIONS_DEFAULT-GROUP_SOCKET__SLACK_BOT__TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "communication-slack",
                            "key": "slack-app-token",
                        },
                    },
                },
            ],
            # Volume configuration
            "extraVolumes": [],
            "extraVolumeMounts": [],
            # Node assignment
            "nodeSelector": {},
            "tolerations": [],
            "affinity": {},
            # Service account
            "serviceAccount": {"create": True, "name": "", "annotations": {}},
            # Extra Kubernetes resources
            "extraObjects": [],
            # Analytics
            "analytics": {"disable": False},
            # Config watcher
            "configWatcher": {
                "enabled": True,
                "inCluster": {"informerResyncPeriod": "10m"},
            },
            # Plugins configuration
            "plugins": {
                "repositories": {
                    "botkube": {
                        "url": "https://github.com/kubeshop/botkube/releases/download/v1.14.0/plugins-index.yaml"
                    },
                    "botkubeExtra": {
                        "url": "https://github.com/kubeshop/botkube-plugins/releases/download/v1.14.0/plugins-index.yaml"
                    },
                },
                "incomingWebhook": {"enabled": True, "port": 2115, "targetPort": 2115},
                "restartPolicy": {"type": "DeactivatePlugin", "threshold": 10},
                "healthCheckInterval": "10s",
            },
            # Remote configuration
            "config": {
                "provider": {
                    "identifier": "",
                    "endpoint": "https://api.botkube.io/graphql",
                    "apiKey": "",
                }
            },
        },
        skip_await=False,
    ),
)
