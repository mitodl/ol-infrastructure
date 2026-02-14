"""Create the infrastructure and services needed to support the MIT XPro application.

- Create a Redis instance in AWS Elasticache
- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
- Optionally deploy the application to Kubernetes (toggle via xpro:k8s_deploy)
"""

import json
import os
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
import pulumiverse_heroku as heroku
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    InvokeOptions,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import ec2, iam

from bridge.lib.magic_numbers import (
    DEFAULT_NGINX_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.xpro.k8s_secrets import (
    create_xpro_k8s_secrets,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8s,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.heroku import setup_heroku_provider
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

if Config("vault_server").get("env_namespace"):
    setup_vault_provider(skip_child_token=True)

xpro_config = Config("xpro")
stack_info = parse_stack()
k8s_deploy = xpro_config.get_bool("k8s_deploy") or False

heroku_config = Config("heroku")

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
aws_config = AWSBase(
    tags={
        "OU": "mitxpro",
        "Environment": f"applications_{stack_info.env_suffix}",
        "Application": "xpro",
    }
)

setup_heroku_provider()

# Create S3 buckets

# Bucket used to store file uploads from xpro app.
xpro_storage_bucket_name = f"ol-xpro-app-{stack_info.env_suffix}"
xpro_storage_bucket_config = S3BucketConfig(
    bucket_name=xpro_storage_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    tags=aws_config.tags,
    bucket_policy_document=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "*"},
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{xpro_storage_bucket_name}/*",
                }
            ],
        }
    ),
)
xpro_storage_bucket = OLBucket(
    "ol-xpro-app",
    config=xpro_storage_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"ol-xpro-app-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="ol-xpro-app-bucket-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="ol-xpro-app-bucket-versioning",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="ol-xpro-app-bucket-public-access",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="ol-xpro-app-bucket-policy",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)


xpro_iam_policy = iam.Policy(
    f"xpro-{stack_info.env_suffix}-policy",
    description=(
        "AWS access controls for the xpro application in the "
        f"{stack_info.name} environment"
    ),
    path=f"/ol-applications/xpro/{stack_info.env_suffix}/",
    name_prefix="aws-permissions-",
    policy=lint_iam_policy(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:ListAllMyBuckets",
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:*MultiPartUpload*",
                        "s3:ListBucket*",
                        "s3:PutObject*",
                        "s3:GetObject*",
                        "s3:DeleteObject*",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::{xpro_storage_bucket_name}",
                        f"arn:aws:s3:::{xpro_storage_bucket_name}/*",
                    ],
                },
            ],
        },
        stringify=True,
        parliament_config={
            "PERMISSIONS_MANAGEMENT_ACTIONS": {
                "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
            },
            "RESOURCE_EFFECTIVELY_STAR": {},
        },
    ),
)

xpro_vault_backend_role = vault.aws.SecretBackendRole(
    "xpro-app",
    name="xpro-app",
    backend="aws-mitx",
    credential_type="iam_user",
    iam_tags={"OU": "operations", "vault_managed": "True"},
    policy_arns=[xpro_iam_policy.arn],
)

# Create RDS instance
db_ingress_rules = [
    ec2.SecurityGroupIngressArgs(
        protocol="tcp",
        from_port=DEFAULT_POSTGRES_PORT,
        to_port=DEFAULT_POSTGRES_PORT,
        security_groups=[data_vpc["security_groups"]["integrator"]],
        cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"],
    ),
    ec2.SecurityGroupIngressArgs(
        protocol="tcp",
        from_port=DEFAULT_POSTGRES_PORT,
        to_port=DEFAULT_POSTGRES_PORT,
        cidr_blocks=["0.0.0.0/0"],
        ipv6_cidr_blocks=["::/0"],
        description="Allow access over the public internet from Heroku",
    ),
]
if k8s_deploy:
    db_ingress_rules.append(
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=apps_vpc["k8s_pod_subnet_cidrs"],
            description="Allow access from the app running in Kubernetes",
        ),
    )

xpro_db_security_group = ec2.SecurityGroup(
    f"xpro-db-access-{stack_info.env_suffix}",
    description=f"Access control for the xpro DB in {stack_info.name}",
    ingress=db_ingress_rules,
    tags=aws_config.merged_tags(
        {"name": f"xpro-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=apps_vpc["id"],
)

xpro_db_config = OLPostgresDBConfig(
    instance_name=f"xpro-db-applications-{stack_info.env_suffix}",
    password=xpro_config.require("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[xpro_db_security_group],
    engine_major_version="17",
    tags=aws_config.tags,
    db_name="xpro",
    public_access=True,
    parameter_overrides=[{"name": "password_encryption", "value": "md5"}],
    **defaults(stack_info)["rds"],
)
xpro_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
)
xpro_db = OLAmazonDB(xpro_db_config)

xpro_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=xpro_db_config.db_name,
    mount_point=f"{xpro_db_config.engine}-xpro",
    db_admin_username=xpro_db_config.username,
    db_admin_password=xpro_db_config.password.get_secret_value(),
    db_host=xpro_db.db_instance.address,
    **defaults(stack_info)["rds"],
)
xpro_vault_backend = OLVaultDatabaseBackend(xpro_vault_backend_config)

######################
# Secrets Management #
######################
# Don't create the mount here because it is already created as part of
# ol_infrastructure/applications/edxapp. Instead we want to factor the creation of all
# mount points to a single substructure project and use stack references for all usages
# to populate or retrieve values in the resepective mounts.
# TMM 2023-11-14

xpro_vault_secrets = read_yaml_secrets(
    Path(f"xpro/secrets.{stack_info.env_suffix}.yaml")
)

for key, data in xpro_vault_secrets.items():
    vault.kv.Secret(
        f"xpro-vault-secrets-{key}",
        # This mount is created already as part of the edxapp Pulumi project. See note
        # above for future work.
        path=f"secret-xpro/{key}",
        data_json=json.dumps(data),
    )

# env_name is 'ci' 'rc' or 'production'
env_name = stack_info.name.lower() if stack_info.name != "QA" else "rc"

# Non-sensitive env vars shared between Heroku and K8s deployments
app_env_vars = {
    "AWS_STORAGE_BUCKET_NAME": xpro_storage_bucket_name,
    "CERTIFICATE_CREATION_DELAY_IN_HOURS": 48,
    "CRON_COURSERUN_SYNC_HOURS": "*",
    "CYBERSOURCE_MERCHANT_ID": "mit_odl_xpro",
    "CYBERSOURCE_REFERENCE_PREFIX": f"xpro-{env_name}",
    "HUBSPOT_PIPELINE_ID": "75e28846-ad0d-4be2-a027-5e1da6590b98",
    "MITOL_DIGITAL_CREDENTIALS_AUTH_TYPE": "code",
    "MITOL_DIGITAL_CREDENTIALS_DEEP_LINK_URL": "dccrequest://request",
    "MITXPRO_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "MITXPRO_DB_CONN_MAX_AGE": 0,
    "MITXPRO_DB_DISABLE_SSL": "True",
    "MITXPRO_EMAIL_TLS": "True",
    "MITXPRO_ENVIRONMENT": env_name,
    "MITXPRO_FROM_EMAIL": "MIT xPRO <support@xpro.mit.edu>",
    "MITXPRO_OAUTH_PROVIDER": "mitxpro-oauth2",
    "MITXPRO_REPLY_TO_ADDRESS": "MIT xPRO <support@xpro.mit.edu>",
    "MITXPRO_SECURE_SSL_REDIRECT": False,
    "MITXPRO_USE_S3": "True",
    "NODE_MODULES_CACHE": "False",
    "OAUTH2_PROVIDER_ALLOWED_REDIRECT_URI_SCHEMES": "http,https,dccrequest",
    "POSTHOG_ENABLED": "True",
    "POSTHOG_API_HOST": "https://ph.ol.mit.edu",
    # This can be removed once PR#1314 is in production,
    "OPENEDX_OAUTH_APP_NAME": "edx-oauth-app",
    # This replaces OPENEDX_GRADES_API_TOKEN and is
    # tied to xpro-grades-api user in openedx,
    "OPENEDX_SERVICE_WORKER_USERNAME": "xpro-service-worker-api",
    "PGBOUNCER_DEFAULT_POOL_SIZE": 50,
    "PGBOUNCER_MIN_POOL_SIZE": 5,
    "SHEETS_DATE_TIMEZONE": "America/New_York",
    "SHEETS_TASK_OFFSET": "120",
    "SHOW_UNREDEEMED_COUPON_ON_DASHBOARD": "True",
    "SITE_NAME": "MIT xPRO",
    "USE_X_FORWARDED_HOST": "True",
}

if k8s_deploy:
    ################################
    # Kubernetes Deployment Path   #
    ################################
    vault_config = Config("vault")
    redis_config = Config("redis")

    cluster_stack = StackReference(
        f"infrastructure.aws.eks.applications.{stack_info.name}"
    )
    cluster_substructure_stack = StackReference(
        f"substructure.aws.eks.applications.{stack_info.name}"
    )
    vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
    k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]

    k8s_app_labels = K8sAppLabels(
        application=Application.xpro,
        product=Product.xpro,
        service=Services.xpro,
        ou=BusinessUnit.xpro,
        source_repository="https://github.com/mitodl/mitxpro",
        stack=stack_info,
    ).model_dump()

    setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
    xpro_namespace = "xpro"
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(xpro_namespace, ns)
    )

    # Pod security group for the xPro application
    xpro_app_security_group = ec2.SecurityGroup(
        f"xpro-app-access-{stack_info.env_suffix}",
        description=f"Access control for the xPro App in {stack_info.name}",
        egress=default_psg_egress_args,
        ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
        tags=aws_config.tags,
        vpc_id=apps_vpc["id"],
    )

    # Redis / Elasticache
    redis_cluster_security_group = ec2.SecurityGroup(
        f"xpro-redis-cluster-security-group-{stack_info.env_suffix}",
        name_prefix=f"xpro-redis-cluster-security-group-{stack_info.env_suffix}",
        description="Access control for the xPro redis cluster.",
        ingress=[
            ec2.SecurityGroupIngressArgs(
                security_groups=[
                    xpro_app_security_group.id,
                    cluster_substructure_stack.require_output(
                        "cluster_keda_security_group_id"
                    ),
                ],
                protocol="tcp",
                from_port=DEFAULT_REDIS_PORT,
                to_port=DEFAULT_REDIS_PORT,
                description="Allow application pods to talk to Redis",
            ),
            ec2.SecurityGroupIngressArgs(
                cidr_blocks=operations_vpc["k8s_pod_subnet_cidrs"],
                protocol="tcp",
                from_port=DEFAULT_REDIS_PORT,
                to_port=DEFAULT_REDIS_PORT,
                description=(
                    "Allow Operations VPC celery monitoring pods to talk to Redis"
                ),
            ),
        ],
        vpc_id=apps_vpc["id"],
        tags=aws_config.tags,
    )
    redis_defaults = defaults(stack_info)["redis"]
    redis_defaults["instance_type"] = (
        redis_config.get("instance_type") or redis_defaults["instance_type"]
    )
    redis_cache_config = OLAmazonRedisConfig(
        encrypt_transit=True,
        auth_token=redis_config.require("password"),
        cluster_mode_enabled=False,
        encrypted=True,
        engine_version="7.2",
        engine="valkey",
        num_instances=3,
        shard_count=1,
        auto_upgrade=True,
        cluster_description="Redis cluster for xPro",
        cluster_name=f"xpro-app-redis-{stack_info.env_suffix}",
        subnet_group=apps_vpc["elasticache_subnet"],
        security_groups=[redis_cluster_security_group.id],
        tags=aws_config.tags,
        **redis_defaults,
    )
    redis_cache = OLAmazonCache(redis_cache_config)

    # Vault policy and K8s auth
    xpro_vault_policy_template = (
        Path(__file__).parent.joinpath("xpro_policy.hcl").read_text()
    )
    xpro_vault_policy = vault.Policy(
        f"xpro-vault-policy-{stack_info.env_suffix}",
        name="xpro",
        policy=xpro_vault_policy_template,
    )

    xpro_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"xpro-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name=Services.xpro,
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=[xpro_namespace],
        token_policies=[xpro_vault_policy.name],
    )

    vault_k8s_resources = OLVaultK8SResources(
        resource_config=OLVaultK8SResourcesConfig(
            application_name=Services.xpro,
            namespace=xpro_namespace,
            labels=k8s_app_labels,
            vault_address=vault_config.require("address"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            vault_auth_role_name=xpro_vault_k8s_auth_backend_role.role_name,
        ),
        opts=ResourceOptions(delete_before_replace=True),
    )

    # RDS endpoint for K8s secret templates
    db_instance_name = f"xpro-db-applications-{stack_info.env_suffix}"
    rds_endpoint = (
        f"{db_instance_name}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com"
        f":{DEFAULT_POSTGRES_PORT}"
    )

    # Create Kubernetes secrets
    secret_names, secret_resources = create_xpro_k8s_secrets(
        stack_info=stack_info,
        xpro_namespace=xpro_namespace,
        k8s_global_labels=k8s_app_labels,
        vault_k8s_resources=vault_k8s_resources,
        db_config=xpro_vault_backend,
        rds_endpoint=rds_endpoint,
        redis_password=redis_config.require("password"),
        redis_cache=redis_cache,
    )

    # Merge stack-level config vars into the app env vars
    app_env_vars.update(**xpro_config.get_object("vars") or {})

    if "XPRO_DOCKER_TAG" not in os.environ:
        msg = "XPRO_DOCKER_TAG must be set."
        raise OSError(msg)
    XPRO_DOCKER_TAG = os.environ["XPRO_DOCKER_TAG"]

    xpro_k8s_app = OLApplicationK8s(
        ol_app_k8s_config=OLApplicationK8sConfig(
            project_root=Path(__file__).parent,
            application_config=app_env_vars,
            application_name=Services.xpro,
            application_namespace=xpro_namespace,
            application_lb_service_name="xpro-webapp",
            application_lb_service_port_name="http",
            application_min_replicas=xpro_config.get_int("min_replicas") or 2,
            k8s_global_labels=k8s_app_labels,
            env_from_secret_names=secret_names,
            application_security_group_id=xpro_app_security_group.id,
            application_security_group_name=xpro_app_security_group.name,
            application_image_repository="mitodl/xpro-app",
            application_docker_tag=XPRO_DOCKER_TAG,
            application_cmd_array=["uwsgi"],
            application_arg_array=["/tmp/uwsgi.ini"],  # noqa: S108
            vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
            registry="dockerhub",
            import_nginx_config=True,
            import_nginx_config_path="files/web.conf",
            import_uwsgi_config=True,
            init_migrations=True,
            init_collectstatic=True,
            pre_deploy_commands=[
                ("migrate", ["python", "manage.py", "migrate", "--noinput"])
            ],
            celery_worker_configs=[
                OLApplicationK8sCeleryWorkerConfig(
                    application_name="mitxpro.celery:app",
                    queue_name="default",
                    redis_host=redis_cache.address,
                    redis_password=redis_config.require("password"),
                ),
            ],
            resource_requests={"cpu": "250m", "memory": "1Gi"},
            resource_limits={"memory": "1Gi"},
            probe_configs={
                "liveness_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/nginx-health",
                        port=DEFAULT_NGINX_PORT,
                    ),
                    initial_delay_seconds=30,
                    period_seconds=30,
                    failure_threshold=3,
                    timeout_seconds=3,
                ),
                "readiness_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/nginx-health",
                        port=DEFAULT_NGINX_PORT,
                    ),
                    initial_delay_seconds=15,
                    period_seconds=15,
                    failure_threshold=3,
                    timeout_seconds=3,
                ),
                "startup_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/nginx-health",
                        port=DEFAULT_NGINX_PORT,
                    ),
                    initial_delay_seconds=10,
                    period_seconds=10,
                    failure_threshold=6,
                    success_threshold=1,
                    timeout_seconds=5,
                ),
            },
        ),
        opts=ResourceOptions(depends_on=[xpro_app_security_group, *secret_resources]),
    )

    # APISIX routing (pass-through, no OIDC)
    app_domain = xpro_config.require("app_domain")
    tls_secret_name = "xpro-tls-pair"  # noqa: S105  # pragma: allowlist secret

    cert_manager_certificate = OLCertManagerCert(
        f"xpro-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="xpro",
            k8s_namespace=xpro_namespace,
            k8s_labels=k8s_app_labels,
            create_apisixtls_resource=True,
            dest_secret_name=tls_secret_name,
            dns_names=[app_domain],
        ),
    )

    xpro_apisix_httproute = OLApisixHTTPRoute(
        f"xpro-apisix-httproute-{stack_info.env_suffix}",
        route_configs=[
            OLApisixHTTPRouteConfig(
                route_name="passthrough",
                hosts=[app_domain],
                paths=["/*"],
                backend_service_name=xpro_k8s_app.application_lb_service_name,
                backend_service_port=xpro_k8s_app.application_lb_service_port_name,
                plugins=[],
            ),
        ],
        k8s_namespace=xpro_namespace,
        k8s_labels=k8s_app_labels,
    )

################################
# Heroku Deployment Path       #
################################
heroku_vars = dict(app_env_vars)
heroku_vars["MITXPRO_SECURE_SSL_REDIRECT"] = "True"
heroku_vars.update(**xpro_config.get_object("vars") or {})

# Finally, populate a map of vars that contain secrets
auth_aws_mitx_creds_xpro_app = vault.generic.get_secret_output(
    path="aws-mitx/creds/xpro-app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=xpro_vault_backend_role),
)
auth_postgres_xpro_creds_app = vault.generic.get_secret_output(
    path="postgres-xpro/creds/app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=xpro_vault_backend_role),
)
secret_global_mailgun_api_key = vault.generic.get_secret_output(
    path="secret-global/mailgun",
    opts=InvokeOptions(parent=xpro_vault_backend_role),
)

sensitive_heroku_vars = {
    # Secrets that can be source locally from SOPS
    "COUPON_REQUEST_SHEET_ID": xpro_vault_secrets["google-sheets"]["sheet_id"],
    "CYBERSOURCE_ACCESS_KEY": xpro_vault_secrets["cybersource"]["access_key"],
    "CYBERSOURCE_INQUIRY_LOG_NACL_ENCRYPTION_KEY": xpro_vault_secrets["cybersource"][
        "inquiry_log_nacl_encryption_key"
    ],
    "CYBERSOURCE_PROFILE_ID": xpro_vault_secrets["cybersource"]["profile_id"],
    "CYBERSOURCE_SECURITY_KEY": xpro_vault_secrets["cybersource"]["security_key"],
    "CYBERSOURCE_TRANSACTION_KEY": xpro_vault_secrets["cybersource"]["transaction_key"],
    "DEFERRAL_REQUEST_WORKSHEET_ID": xpro_vault_secrets["google-sheets"][
        "deferral_worksheet_id"
    ],
    "DIGITAL_CREDENTIALS_ISSUER_ID": xpro_vault_secrets["digital-credentials"][
        "issuer_id"
    ],
    "DIGITAL_CREDENTIALS_OAUTH2_CLIENT_ID": xpro_vault_secrets["digital-credentials"][
        "oauth2_client_id"
    ],
    "DIGITAL_CREDENTIALS_VERIFICATION_METHOD": xpro_vault_secrets[
        "digital-credentials"
    ]["verification_method"],
    "DRIVE_OUTPUT_FOLDER_ID": xpro_vault_secrets["google-sheets"]["folder_id"],
    "DRIVE_SERVICE_ACCOUNT_CREDS": xpro_vault_secrets["google-sheets"][
        "service_account_creds"
    ],
    "DRIVE_SHARED_ID": xpro_vault_secrets["google-sheets"]["drive_shared_id"],
    "EMERITUS_API_KEY": xpro_vault_secrets["emeritus"]["api_key"],
    "ENROLLMENT_CHANGE_SHEET_ID": xpro_vault_secrets["google-sheets"][
        "enroll_change_sheet_id"
    ],
    "EXTERNAL_COURSE_SYNC_API_KEY": xpro_vault_secrets["external-course-sync"][
        "api_key"
    ],
    "EXTERNAL_COURSE_SYNC_EMAIL_RECIPIENTS": xpro_vault_secrets["external-course-sync"][
        "email-recipients"
    ],
    "HIREFIRE_TOKEN": xpro_vault_secrets["hirefire"]["token"],
    "MITOL_DIGITAL_CREDENTIALS_HMAC_SECRET": xpro_vault_secrets["digital-credentials"][
        "hmac_secret"
    ],
    "MITOL_DIGITAL_CREDENTIALS_VERIFY_SERVICE_BASE_URL": xpro_vault_secrets[
        "digital-credentials"
    ]["sign_and_verify_url"],
    "MITOL_HUBSPOT_API_PRIVATE_TOKEN": xpro_vault_secrets["hubspot"][
        "api_private_token"
    ],
    "MITXPRO_EMAIL_HOST": xpro_vault_secrets["smtp"]["relay_host"],
    "MITXPRO_EMAIL_PASSWORD": xpro_vault_secrets["smtp"]["relay_password"],
    "MITXPRO_EMAIL_PORT": xpro_vault_secrets["smtp"]["relay_port"],
    "MITXPRO_EMAIL_USER": xpro_vault_secrets["smtp"]["relay_username"],
    "MITXPRO_REGISTRATION_ACCESS_TOKEN": xpro_vault_secrets["openedx"][
        "registration_access_token"
    ],
    "MITXPRO_SUPPORT_EMAIL": xpro_vault_secrets["smtp"]["support_email"],
    "OPENEDX_API_CLIENT_ID": xpro_vault_secrets["openedx-api-client"]["client_id"],
    "OPENEDX_API_CLIENT_SECRET": xpro_vault_secrets["openedx-api-client"][
        "client_secret"
    ],
    "OPENEDX_GRADES_API_TOKEN": xpro_vault_secrets["openedx"]["grades_api_token"],
    "OPENEDX_SERVICE_WORKER_API_TOKEN": xpro_vault_secrets["openedx"][
        "service_worker_api_token"
    ],
    "POSTHOG_PROJECT_API_KEY": xpro_vault_secrets.get("posthog", {}).get(
        "project_api_key", ""
    ),
    "RECAPTCHA_SECRET_KEY": xpro_vault_secrets["recaptcha"]["secret_key"],
    "RECAPTCHA_SITE_KEY": xpro_vault_secrets["recaptcha"]["site_key"],
    "REFUND_REQUEST_WORKSHEET_ID": xpro_vault_secrets["google-sheets"][
        "refund_worksheet_id"
    ],
    "SECRET_KEY": xpro_vault_secrets["django"]["secret-key"],
    "SENTRY_DSN": xpro_vault_secrets["sentry"]["dsn"],
    "SHEETS_ADMIN_EMAILS": xpro_vault_secrets["google-sheets"]["admin_emails"],
    "STATUS_TOKEN": xpro_vault_secrets["django"]["status-token"],
    "VOUCHER_DOMESTIC_AMOUNT_KEY": xpro_vault_secrets["voucher-domestic"]["amount_key"],
    "VOUCHER_DOMESTIC_COURSE_KEY": xpro_vault_secrets["voucher-domestic"]["course_key"],
    "VOUCHER_DOMESTIC_CREDITS_KEY": xpro_vault_secrets["voucher-domestic"][
        "credits_key"
    ],
    "VOUCHER_DOMESTIC_DATES_KEY": xpro_vault_secrets["voucher-domestic"]["dates_key"],
    "VOUCHER_DOMESTIC_DATE_KEY": xpro_vault_secrets["voucher-domestic"]["date_key"],
    "VOUCHER_DOMESTIC_EMPLOYEE_ID_KEY": xpro_vault_secrets["voucher-domestic"][
        "employee_id_key"
    ],
    "VOUCHER_DOMESTIC_EMPLOYEE_KEY": xpro_vault_secrets["voucher-domestic"][
        "employee_key"
    ],
    "VOUCHER_DOMESTIC_KEY": xpro_vault_secrets["voucher-domestic"]["key"],
    # Auth secrets that require something more involved
    "AWS_ACCESS_KEY_ID": auth_aws_mitx_creds_xpro_app.data.apply(
        lambda data: "{}".format(data["access_key"])
    ),
    "AWS_SECRET_ACCESS_KEY": auth_aws_mitx_creds_xpro_app.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "DATABASE_URL": auth_postgres_xpro_creds_app.data.apply(
        lambda data: (
            "postgres://{}:{}@xpro-db-applications-{}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432/xpro".format(
                data["username"], data["password"], stack_info.name.lower()
            )
        )
    ),
    # Static secrets that require something more involved
    "MAILGUN_KEY": secret_global_mailgun_api_key.data.apply(
        lambda data: "{}".format(data["api_key"])
    ),
}


# Put it all together into a ConfigAssociation resource
heroku_app_id = heroku_config.require("app_id")
xpro_heroku_configassociation = heroku.app.ConfigAssociation(
    f"xpro-{stack_info.env_suffix}-heroku-configassociation",
    app_id=heroku_app_id,
    sensitive_vars=sensitive_heroku_vars,
    vars=heroku_vars,
)

export(
    "xpro_app",
    {
        "rds_host": xpro_db.db_instance.address,
    },
)
