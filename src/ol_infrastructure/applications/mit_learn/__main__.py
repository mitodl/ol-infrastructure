# ruff: noqa: ERA001, FIX002, E501

import base64
import json
import mimetypes
import os
import textwrap
from pathlib import Path
from string import Template

import pulumi_consul as consul
import pulumi_fastly as fastly
import pulumi_github as github
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Alias, Config, InvokeOptions, ResourceOptions, StackReference, export
from pulumi.output import Output
from pulumi_aws import ec2, iam, route53, s3

from bridge.lib.constants import FASTLY_A_TLS_1_3, FASTLY_CNAME_TLS_1_3
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
    DEFAULT_WSGI_PORT,
    ONE_MEGABYTE_BYTE,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.mit_learn.k8s_secrets import (
    create_mitlearn_k8s_secrets,
)
from ol_infrastructure.components.aws.cache import (
    OLAmazonCache,
    OLAmazonRedisConfig,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
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
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
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

setup_vault_provider(skip_child_token=True)
fastly_provider = get_fastly_provider()
github_provider = github.Provider(
    "github-provider",
    owner=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["owner"],
    token=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["token"],
)

mitlearn_config = Config("mitlearn")
vault_config = Config("vault")

stack_info = parse_stack()

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
cluster_substructure_stack = StackReference(
    f"substructure.aws.eks.applications.{stack_info.name}"
)
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]

vector_log_proxy_stack = StackReference(
    f"infrastructure.vector_log_proxy.operations.{stack_info.name}"
)
monitoring_stack = StackReference("infrastructure.monitoring")
dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
learn_zone_id = dns_stack.require_output("learn")["id"]

learn_frontend_domain = mitlearn_config.require("frontend_domain")
legacy_learn_frontend_domain = mitlearn_config.require("legacy_frontend_domain")
nextjs_heroku_domain = mitlearn_config.require("nextjs_heroku_domain")
mitlearn_api_domain = mitlearn_config.require("api_domain")

aws_config = AWSBase(
    tags={
        "OU": "mit-open",
        "Environment": stack_info.env_suffix,
        "Application": Services.mit_learn,
    }
)
app_env_suffix = {"ci": "ci", "qa": "rc", "production": "production"}[
    stack_info.env_suffix
]

k8s_global_labels = K8sGlobalLabels(
    service=Services.mit_learn,
    ou=BusinessUnit.mit_learn,
    stack=stack_info,
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
learn_namespace = "mitlearn"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(learn_namespace, ns)
)

#######################################################
# begin legacy block - app bucket config
#######################################################
legacy_app_storage_bucket_name = f"ol-mitopen-app-storage-{app_env_suffix}"
legacy_application_storage_bucket = s3.Bucket(
    f"ol_mitopen_app_storage_bucket_{stack_info.env_suffix}",
    bucket=legacy_app_storage_bucket_name,
    tags=aws_config.tags,
)

s3.BucketVersioning(
    "ol-mitopen-bucket-versioning",
    bucket=legacy_application_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
legacy_app_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-mitopen-bucket-ownership-controls",
    bucket=legacy_application_storage_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
legacy_app_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-mitopen-bucket-public-access",
    bucket=legacy_application_storage_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)

s3.BucketPolicy(
    "ol-mitopen-bucket-policy",
    bucket=legacy_application_storage_bucket.id,
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{legacy_app_storage_bucket_name}/*"],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            legacy_app_bucket_public_access,
            legacy_app_bucket_ownership_controls,
        ]
    ),
)
#######################################################
# end legacy block - app bucket config
#######################################################

mitlearn_app_storage_bucket_name = f"ol-mitlearn-app-storage-{app_env_suffix}"
mitlearn_application_storage_bucket = s3.Bucket(
    f"ol_mitlearn_app_storage_bucket_{stack_info.env_suffix}",
    bucket=mitlearn_app_storage_bucket_name,
    tags=aws_config.tags,
)

s3.BucketVersioning(
    "ol-mitlearn-bucket-versioning",
    bucket=mitlearn_application_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
app_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-mitlearn-bucket-ownership-controls",
    bucket=mitlearn_application_storage_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
app_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-mitlearn-bucket-public-access",
    bucket=mitlearn_application_storage_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)

s3.BucketPolicy(
    "ol-mitlearn-bucket-policy",
    bucket=mitlearn_application_storage_bucket.id,
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{mitlearn_app_storage_bucket_name}/*"],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            app_bucket_public_access,
            app_bucket_ownership_controls,
        ]
    ),
)

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    },
    "RESOURCE_EFFECTIVELY_STAR": {},
    "RESOURCE_MISMATCH": {},
}

################################
# Remove legacy bucket arns
################################
gh_workflow_s3_bucket_permissions = [
    {
        "Action": [
            "s3:ListBucket*",
        ],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::{legacy_app_storage_bucket_name}",
            f"arn:aws:s3:::{mitlearn_app_storage_bucket_name}",
        ],
    },
    {
        "Action": [
            "s3:GetObject*",
            "s3:PutObject",
            "s3:PutObjectAcl",
            "s3:DeleteObject",
        ],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::{legacy_app_storage_bucket_name}/frontend/*",
            f"arn:aws:s3:::{mitlearn_app_storage_bucket_name}/frontend/*",
        ],
    },
]

gh_workflow_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": gh_workflow_s3_bucket_permissions,
}

gh_workflow_iam_policy = iam.Policy(
    f"ol_mitopen_gh_workflow_iam_permissions_{stack_info.env_suffix}",
    name=f"ol-mitlearn-gh-workflow-permissions-{stack_info.env_suffix}",
    path=f"/ol-applications/mitlearn/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        gh_workflow_policy_document, stringify=True, parliament_config=parliament_config
    ),
)

# Just create a static user for now. Some day refactor to use
# https://github.com/hashicorp/vault-action
gh_workflow_user = iam.User(
    f"ol_mitopen_gh_workflow_user_{stack_info.env_suffix}",
    name=f"mitopen-gh-workflow-{stack_info.env_suffix}",
    tags=aws_config.tags,
)
iam.PolicyAttachment(
    f"ol_mitopen_gh_workflow_user_{stack_info.env_suffix}",
    policy_arn=gh_workflow_iam_policy.arn,
    users=[gh_workflow_user.name],
)
gh_workflow_accesskey = iam.AccessKey(
    f"ol_mitopen_gh_workflow_accesskey-{stack_info.env_suffix}",
    user=gh_workflow_user.name,
    status="Active",
)

################################
# Remove legacy bucket arns
################################
application_s3_bucket_permissions = [
    {
        "Action": [
            "s3:GetObject*",
            "s3:ListBucket*",
            "s3:PutObject",
            "s3:PutObjectAcl",
            "s3:DeleteObject",
        ],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::odl-discussions-{app_env_suffix}",
            f"arn:aws:s3:::odl-discussions-{app_env_suffix}/*",
            f"arn:aws:s3:::{legacy_app_storage_bucket_name}",
            f"arn:aws:s3:::{legacy_app_storage_bucket_name}/*",
            f"arn:aws:s3:::{mitlearn_app_storage_bucket_name}",
            f"arn:aws:s3:::{mitlearn_app_storage_bucket_name}/*",
        ],
    },
    {
        "Action": ["s3:GetObject*", "s3:ListBucket*"],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::edxorg-{stack_info.env_suffix}-edxapp-courses",
            f"arn:aws:s3:::edxorg-{stack_info.env_suffix}-edxapp-courses/*",
            "arn:aws:s3:::mitx-etl-xpro-production-mitxpro-production",
            "arn:aws:s3:::mitx-etl-xpro-production-mitxpro-production/*",
            "arn:aws:s3:::mitx-etl-mitxonline-production",
            "arn:aws:s3:::mitx-etl-mitxonline-production/*",
            "arn:aws:s3:::ol-data-lake-landing-zone-*",
            "arn:aws:s3:::ol-data-lake-landing-zone-*/*",
            "arn:aws:s3:::ol-olx-course-exports",
            "arn:aws:s3:::ol-olx-course-exports/*",
            "arn:aws:s3:::ocw-content-storage",
            "arn:aws:s3:::ocw-content-storage/*",
            f"arn:aws:s3:::ol-ocw-studio-app-{app_env_suffix}",
        ],
    },
]

application_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": application_s3_bucket_permissions,
}

mitlearn_iam_policy = iam.Policy(
    f"ol_mitopen_iam_permissions_{stack_info.env_suffix}",
    name=f"ol-mitopen-application-permissions-{stack_info.env_suffix}",
    path=f"/ol-applications/mitopen/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        application_policy_document, stringify=True, parliament_config=parliament_config
    ),
)

# Begin vault resources
mitlearn_vault_iam_role = vault.aws.SecretBackendRole(
    f"ol-mitopen-iam-permissions-vault-policy-{stack_info.env_suffix}",
    name="ol-mitlearn-application",
    backend="aws-mitx",
    credential_type="iam_user",
    iam_tags={"OU": "operations", "vault_managed": "True"},
    policy_arns=[mitlearn_iam_policy.arn],
)

mitlearn_vault_mount = vault.Mount(
    f"ol-mitlearn-configuration-secrets-mount-{stack_info.env_suffix}",
    path="secret-mitlearn",
    type="kv-v2",
    options={"version": 2},
    description="Storage of configuration secrets used by MIT-Learn",
    opts=ResourceOptions(
        delete_before_replace=True, depends_on=[mitlearn_vault_iam_role]
    ),
)

# There is a reason, I think, why these are still at `bridge/secrets/mitopen`
# and not `bridge/secrets/mitlearn` -- Open Discussions
mitlearn_vault_secrets = read_yaml_secrets(
    Path(f"mitlearn/secrets.{stack_info.env_suffix}.yaml"),
)

mitlearn_vault_static_secrets = vault.generic.Secret(
    f"ol-mitlearn-configuration-secrets-{stack_info.env_suffix}",
    path=mitlearn_vault_mount.path.apply("{}/secrets".format),
    data_json=json.dumps(mitlearn_vault_secrets),
)

# The policy has been updated to allow for reading from the old or
# the new mount.
mitlearn_vault_policy = vault.Policy(
    f"ol-mitlearn-vault-policy-{stack_info.env_suffix}",
    name="mitlearn",
    policy=Path(__file__).parent.joinpath("mitlearn_policy.hcl").read_text(),
)

mitlearn_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"ol-mitlearn-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
    role_name="mitlearn",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[learn_namespace],
    token_policies=[mitlearn_vault_policy.name],
)

vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="mitlearn",
    namespace=learn_namespace,
    labels=k8s_global_labels,
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=mitlearn_vault_k8s_auth_backend_role.role_name,
)

vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[mitlearn_vault_k8s_auth_backend_role],
    ),
)

### End vault resources
# Create a security group for the application pods
mitlearn_app_security_group = ec2.SecurityGroup(
    f"mitlearn-app-sg-{stack_info.env_suffix}",
    name=f"mitlearn-app-sg-{stack_info.env_suffix}",
    description="Security group for mitlearn application pods",
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
    tags=aws_config.tags,
    vpc_id=apps_vpc["id"],
)

mitlearn_db_security_group = ec2.SecurityGroup(
    f"ol-mitopen-db-access-{stack_info.env_suffix}",
    description=f"Access control for the MIT Open application DB in {stack_info.name}",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            security_groups=[
                mitlearn_app_security_group.id,
                data_vpc["security_groups"]["orchestrator"],
                data_vpc["security_groups"]["integrator"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            # Airbyte isn't using pod security groups in Kubernetes. This is a
            # workaround to allow for data integration from the data Kubernetes
            # cluster. (TMM 2025-05-16)
            cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"].apply(
                lambda pod_cidrs: [
                    # Grant access from Hightouch for certificate sync
                    "54.196.30.169/32",
                    "52.72.201.213/32",
                    "18.213.226.96/32",
                    "3.224.126.197/32",
                    "3.217.26.199/32",
                    # This is in order to allow Vault to resolve the connection over the
                    # public internet due to being located in a separate VPC. The public
                    # access is toggled to ON because of Hightouch, so the default
                    # resolution outside of the VPC that the DB lives in is to go over
                    # the public net. Because Vault couldn't reach the DB it couldn't
                    # create credentials. (TMM 2025-09-08)
                    "0.0.0.0/0",
                    *pod_cidrs,
                ]
            ),
            description="Allow access over the public internet from Heroku.",
        )
    ],
    egress=[
        ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/32"],
            ipv6_cidr_blocks=["::/0"],
        )
    ],
    tags=aws_config.tags,
    vpc_id=apps_vpc["id"],
)

rds_password = mitlearn_config.require("db_password")
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    mitlearn_config.get("db_instance_size") or rds_defaults["instance_size"]
)
mitlearn_db_config = OLPostgresDBConfig(
    instance_name=f"ol-mitlearn-db-{stack_info.env_suffix}",
    password=rds_password,
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[mitlearn_db_security_group],
    engine_major_version="15",
    tags=aws_config.tags,
    db_name="mitopen",
    public_access=True,
    **rds_defaults,
)
mitlearn_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
)

mitlearn_db = OLAmazonDB(
    db_config=mitlearn_db_config,
    opts=ResourceOptions(aliases=[Alias(f"ol-mitopen-db-{stack_info.env_suffix}")]),
)

mitlearn_role_statements = postgres_role_statements.copy()
mitlearn_role_statements.pop("app")
mitlearn_role_statements["app"] = {
    "create": [
        # Check if the mitopen role exists and create it if not
        Template(
            """
            DO
            $$do$$
            BEGIN
               IF EXISTS (
                  SELECT FROM pg_catalog.pg_roles
                  WHERE  rolname = 'mitopen') THEN
                      RAISE NOTICE 'Role "mitopen" already exists. Skipping.';
               ELSE
                  BEGIN   -- nested block
                     CREATE ROLE mitopen;
                  EXCEPTION
                     WHEN duplicate_object THEN
                        RAISE NOTICE 'Role "mitopen" was just created by a concurrent transaction. Skipping.';
                  END;
               END IF;
            END
            $$do$$;
            """
        ),
        # Create the external schema if it doesn't exist already
        Template("""CREATE SCHEMA IF NOT EXISTS external;"""),
        # Do grants on to the mitopen in both schemas
        Template("""GRANT CREATE ON SCHEMA public TO mitopen WITH GRANT OPTION;"""),
        Template(
            """
            GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "mitopen"
            WITH GRANT OPTION;
            """
        ),
        Template("""GRANT USAGE ON SCHEMA external TO mitopen WITH GRANT OPTION;"""),
        Template(
            """GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA external TO "mitopen";"""
        ),
        Template("""GRANT CREATE ON DATABASE \"mitopen\" TO mitopen;"""),
        Template(
            """
            GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "mitopen"
            WITH GRANT OPTION;
            """
        ),
        Template("""GRANT CREATE ON SCHEMA external TO mitopen WITH GRANT OPTION;"""),
        Template(
            """
            GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA external TO "mitopen"
            WITH GRANT OPTION;
            """
        ),
        Template(
            # Set/refresh default privileges in both schemas
            """
            GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA external TO "mitopen"
            WITH GRANT OPTION;
            """
        ),
        # Set/refresh default privileges in both schemas
        Template("""SET ROLE "mitopen";"""),
        Template(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE "mitopen" IN SCHEMA public
            GRANT ALL PRIVILEGES ON TABLES TO "mitopen" WITH GRANT OPTION;
            """
        ),
        Template(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE "mitopen" IN SCHEMA public
            GRANT ALL PRIVILEGES ON SEQUENCES TO "mitopen" WITH GRANT OPTION;
            """
        ),
        Template(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE "mitopen" IN SCHEMA external
            GRANT ALL PRIVILEGES ON TABLES TO "mitopen" WITH GRANT OPTION;
            """
        ),
        Template(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE "mitopen" IN SCHEMA external
            GRANT ALL PRIVILEGES ON SEQUENCES TO "mitopen" WITH GRANT OPTION;
            """
        ),
        Template("""RESET ROLE;"""),
        # Actually create the user in the 'mitopen' role
        Template(
            """
            CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
            VALID UNTIL '{{expiration}}' IN ROLE "mitopen" INHERIT;
            """
        ),
        # Make sure things done by the new user belong to role and not the user
        Template("""ALTER ROLE "{{name}}" SET ROLE "mitopen";"""),
    ],
    "revoke": [
        # Remove the user from the mitopen role
        Template("""REVOKE "mitopen" FROM "{{name}}";"""),
        # Put the user back into the app role but as an administrator
        Template("""GRANT "{{name}}" TO mitopen WITH ADMIN OPTION;"""),
        # Change ownership to the app role for anything that might belong to this user
        Template("""SET ROLE mitopen;"""),
        Template("""REASSIGN OWNED BY "{{name}}" TO "mitopen";"""),
        Template("""RESET ROLE;"""),
        # Take any permissions assigned directly to this user away
        Template(
            """REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";"""
        ),
        Template(
            """REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA external FROM "{{name}}";"""
        ),
        Template(
            """REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";"""
        ),
        Template(
            """REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA external FROM "{{name}}";"""
        ),
        Template("""REVOKE USAGE ON SCHEMA public FROM "{{name}}";"""),
        Template("""REVOKE USAGE ON SCHEMA external FROM "{{name}}";"""),
        # Finally, drop this user from the database
        Template("""DROP USER "{{name}}";"""),
    ],
    "renew": [],
    "rollback": [],
}
mitlearn_role_statements["reverse-etl"] = {
    "create": [
        # Check if the reverse_etl role exists and create it if not
        Template(
            """
            DO
            $$do$$
            BEGIN
               IF EXISTS (
                  SELECT FROM pg_catalog.pg_roles
                  WHERE  rolname = 'reverse_etl' THEN
                      RAISE NOTICE 'Role "reverse_etl" already exists. Skipping.';
               ELSE
                  BEGIN   -- nested block
                     CREATE ROLE reverse_etl;
                  EXCEPTION
                     WHEN duplicate_object THEN
                        RAISE NOTICE 'Role "reverse_etl" was just created by a concurrent transaction. Skipping.';
                  END;
               END IF;
            END
            $$do$$;
            """
        ),
        # Create the external schema if it doesn't exist already
        Template("""CREATE SCHEMA IF NOT EXISTS external;"""),
        Template(
            """GRANT CREATE ON SCHEMA external TO reverse_etl WITH GRANT OPTION;"""
        ),
        Template(
            """
            GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA external TO "reverse_etl"
            WITH GRANT OPTION;
            """
        ),
        Template(
            """
            GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA external TO "reverse_etl"
            WITH GRANT OPTION;
            """
        ),
        # Set/refresh default privileges in both schemas
        Template(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE "reverse_etl" IN SCHEMA external
            GRANT ALL PRIVILEGES ON TABLES TO "reverse_etl" WITH GRANT OPTION;
            """
        ),
        Template(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE "reverse_etl" IN SCHEMA external
            GRANT ALL PRIVILEGES ON SEQUENCES TO "reverse_etl" WITH GRANT OPTION;
            """
        ),
        Template("""RESET ROLE;"""),
        # Actually create the user in the 'reverse_etl' role
        Template(
            """
            CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
            VALID UNTIL '{{expiration}}' IN ROLE "reverse_etl" INHERIT;
            """
        ),
        # Make sure things done by the new user belong to role and not the user
        Template("""ALTER ROLE "{{name}}" SET ROLE "reverse_etl";"""),
    ],
    "revoke": [
        # Remove the user from the reverse_etl role
        Template("""REVOKE "reverse_etl" FROM "{{name}}";"""),
        # Put the user back into the app role but as an administrator
        Template("""GRANT "{{name}}" TO reverse_etl WITH ADMIN OPTION;"""),
        # Change ownership to the app role for anything that might belong to this user
        Template("""SET ROLE reverse_etl;"""),
        Template("""REASSIGN OWNED BY "{{name}}" TO "reverse_etl";"""),
        Template("""RESET ROLE;"""),
        # Take any permissions assigned directly to this user away
        Template(
            """REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA external FROM "{{name}}";"""
        ),
        Template(
            """REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA external FROM "{{name}}";"""
        ),
        Template("""REVOKE USAGE ON SCHEMA external FROM "{{name}}";"""),
        # Finally, drop this user from the database
        Template("""DROP USER "{{name}}";"""),
    ],
    "renew": [],
    "rollback": [],
}

mitlearn_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=mitlearn_db_config.db_name,
    mount_point=f"{mitlearn_db_config.engine}-mitlearn",
    db_admin_username=mitlearn_db_config.username,
    db_admin_password=rds_password,
    db_host=mitlearn_db.db_instance.address,
    role_statements=mitlearn_role_statements,
)
mitlearn_vault_backend = OLVaultDatabaseBackend(mitlearn_vault_backend_config)


vector_log_proxy_secrets = read_yaml_secrets(
    Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
)
fastly_proxy_credentials = vector_log_proxy_secrets["fastly"]
encoded_fastly_proxy_credentials = base64.b64encode(
    f"{fastly_proxy_credentials['username']}:{fastly_proxy_credentials['password']}".encode()
).decode("utf8")
vector_log_proxy_fqdn = vector_log_proxy_stack.require_output("vector_log_proxy")[
    "fqdn"
]

fastly_access_logging_bucket = monitoring_stack.require_output(
    "fastly_access_logging_bucket"
)
fastly_access_logging_iam_role = monitoring_stack.require_output(
    "fastly_access_logging_iam_role"
)
gzip_settings: dict[str, set[str]] = {"extensions": set(), "content_types": set()}
for k, v in mimetypes.types_map.items():
    if k in (
        ".json",
        ".pdf",
        ".jpeg",
        ".jpg",
        ".html",
        ".css",
        ".js",
        ".svg",
        ".png",
        ".gif",
        ".xml",
        ".vtt",
        ".srt",
    ):
        gzip_settings["extensions"].add(k.strip("."))
        gzip_settings["content_types"].add(v)
mitlearn_fastly_service = fastly.ServiceVcl(
    f"fastly-{stack_info.env_prefix}-{stack_info.env_suffix}",
    name=f"MIT Learn {stack_info.env_suffix}",
    comment="Managed by Pulumi",
    backends=[
        fastly.ServiceVclBackendArgs(
            address=nextjs_heroku_domain,
            name="NextJS_Frontend",
            override_host=nextjs_heroku_domain,
            port=DEFAULT_HTTPS_PORT,
            ssl_cert_hostname=nextjs_heroku_domain,
            ssl_sni_hostname=nextjs_heroku_domain,
            use_ssl=True,
        ),
    ],
    gzips=[
        fastly.ServiceVclGzipArgs(
            name="enable-gzip-compression",
            extensions=list(gzip_settings["extensions"]),
            content_types=list(gzip_settings["content_types"]),
        )
    ],
    product_enablement=fastly.ServiceVclProductEnablementArgs(
        brotli_compression=True,
    ),
    cache_settings=[],
    conditions=[],
    dictionaries=[
        fastly.ServiceVclDictionaryArgs(name="path_redirects"),  # path level redirects
    ],
    domains=[
        fastly.ServiceVclDomainArgs(
            comment=f"{stack_info.env_prefix} {stack_info.env_suffix} Application",
            name=learn_frontend_domain,
        ),
        fastly.ServiceVclDomainArgs(
            comment=f"{stack_info.env_prefix} {stack_info.env_suffix} Application - Legacy",
            name=legacy_learn_frontend_domain,
        ),
    ],
    headers=[
        fastly.ServiceVclHeaderArgs(
            action="set",
            destination="http.Strict-Transport-Security",
            name="Generated by force TLS and enable HSTS",
            source='"max-age=300"',
            type="response",
        ),
    ],
    request_settings=[
        fastly.ServiceVclRequestSettingArgs(
            force_ssl=True,
            name="Generated by force TLS and enable HSTS, change hash keys for prerender.io",
            hash_keys="req.url, req.http.host",
            xff="",
        ),
    ],
    snippets=[
        fastly.ServiceVclSnippetArgs(
            name="handle domain redirect",
            content=textwrap.dedent(
                rf"""
                set req.http.orig-req-url = req.url;
                unset req.http.Cookie;

                # If the request is for the old DNS name, redirect
                if (req.http.host == "{mitlearn_config.require("legacy_frontend_domain")}") {{
                  error 618 "redirect-host";
                }}
                """
            ),
            type="recv",
            priority=10,
        ),
        fastly.ServiceVclSnippetArgs(
            name="Redirect for to correct domain",
            content=textwrap.dedent(
                rf"""
                # redirect to the correct host/domain
                if (obj.status == 618 && obj.response == "redirect-host") {{
                  set obj.status = 302;
                  set obj.http.Location = "https://" + "{mitlearn_config.require("frontend_domain")}" + req.url.path + if (std.strlen(req.url.qs) > 0, "?" req.url.qs, "");
                  return (deliver);
                }}
                """
            ),
            type="error",
        ),
        fastly.ServiceVclSnippetArgs(
            name="handle route dictionary redirect",
            content=Path(__file__)
            .parent.joinpath("snippets/redirect_recv.vcl")
            .read_text(),
            type="recv",
            priority=10,
        ),
        fastly.ServiceVclSnippetArgs(
            name="deliver route dictionary redirect",
            content=Path(__file__)
            .parent.joinpath("snippets/redirect_deliver.vcl")
            .read_text(),
            type="error",
        ),
    ],
    logging_https=[
        fastly.ServiceVclLoggingHttpArgs(
            url=Output.all(fqdn=vector_log_proxy_fqdn).apply(
                lambda fqdn: "https://{fqdn}".format(**fqdn)
            ),
            name=f"fastly-{stack_info.env_prefix}-{stack_info.env_suffix}-https-logging-args",
            content_type="application/json",
            format=build_fastly_log_format_string(additional_static_fields={}),
            format_version=2,
            header_name="Authorization",
            header_value=f"Basic {encoded_fastly_proxy_credentials}",
            json_format="0",
            method="POST",
            request_max_bytes=ONE_MEGABYTE_BYTE,
        )
    ],
    opts=ResourceOptions.merge(
        fastly_provider,
        ResourceOptions(
            aliases=[
                Alias(name=f"fastly-mitopen-{stack_info.env_suffix}"),
                Alias(name=f"fastly-mitlearn-{stack_info.env_suffix}"),
            ],
        ),
    ),
)

path_redirects_dict_id = mitlearn_fastly_service.dictionaries.apply(
    lambda dicts: str(
        next(d.dictionary_id for d in (dicts or []) if d.name == "path_redirects")
    )
)
mitlearn_redirects_dictionary = fastly.ServiceDictionaryItems(
    "mitlearn-redirects-dictionary",
    dictionary_id=path_redirects_dict_id,
    items={"/dashboard/organization/mit": "/dashboard/organization/mit-universal-ai"},
    service_id=mitlearn_fastly_service.id,
    manage_items=True,
    opts=fastly_provider,
)


five_minutes = 60 * 5
route53.Record(
    "ol-mitopen-frontend-dns-record",
    name=mitlearn_config.require("frontend_domain"),
    allow_overwrite=True,
    type="A",
    ttl=five_minutes,
    records=[str(addr) for addr in FASTLY_A_TLS_1_3],
    zone_id=learn_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)
route53.Record(
    "ol-mitopen-frontend-dns-record-legacy",
    name=mitlearn_config.require("legacy_frontend_domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[FASTLY_CNAME_TLS_1_3],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)

# ci, rc, or production
env_name = stack_info.name.lower() if stack_info.name != "QA" else "rc"

# Values that are generally unchanging across environments
env_vars = {
    "ALLOWED_HOSTS": '["*"]',
    "AWS_STORAGE_BUCKET_NAME": f"ol-mitlearn-app-storage-{env_name}",
    "CANVAS_PDF_TRANSCRIPTION_MODEL": "gpt-4o",
    "CORS_ALLOWED_ORIGIN_REGEXES": "['^.+ocw-next.netlify.app$']",
    "CSAIL_BASE_URL": "https://cap.csail.mit.edu/",
    "CSRF_COOKIE_DOMAIN": f".{mitlearn_config.get('frontend_domain')}",
    "EDX_API_ACCESS_TOKEN_URL": "https://api.edx.org/oauth2/v1/access_token",
    "EDX_API_URL": "https://api.edx.org/catalog/v1/catalogs/10/courses",
    "EDX_PROGRAMS_API_URL": "https://discovery.edx.org/api/v1/programs/",
    "MICROMASTERS_CATALOG_API_URL": "https://micromasters.mit.edu/api/v0/catalog/",
    "MICROMASTERS_CMS_API_URL": "https://micromasters.mit.edu/api/v0/wagtail/",
    "MITOL_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "MITOL_AXIOS_BASE_PATH": f"https://{mitlearn_config.get('frontend_domain')}",
    "MITOL_DB_CONN_MAX_AGE": 0,
    "MITOL_DB_DISABLE_SSL": "True",
    "MITOL_SECURE_SSL_REDIRECT": "False",
    "QDRANT_ENABLE_INDEXING_PLUGIN_HOOKS": True,
    "MITOL_DEFAULT_SITE_KEY": "micromasters",
    "MITOL_EMAIL_PORT": 587,
    "MITOL_EMAIL_TLS": "True",
    "EMBEDDINGS_EXTERNAL_FETCH_USE_WEBDRIVER": True,
    "MITOL_ENVIRONMENT": env_name,
    "MITOL_FROM_EMAIL": "MIT Learn <mitopen-support@mit.edu>",
    "MITOL_FRONTPAGE_DIGEST_MAX_POSTS": 10,
    "MITOL_USE_S3": "True",
    "MITOL_NOTIFICATION_EMAIL_BACKEND": "anymail.backends.mailgun.EmailBackend",
    "MITPE_BASE_URL": "https://professional.mit.edu/",
    "MITX_ONLINE_LEARNING_COURSE_BUCKET_NAME": "mitx-etl-mitxonline-production",
    "NEW_RELIC_LOG": "stdout",
    "NODE_MODULES_CACHE": "False",
    "OCW_BASE_URL": "https://ocw.mit.edu/",
    "LITELLM_TOKEN_ENCODING_NAME": "cl100k_base",
    "OCW_CONTENT_BUCKET_NAME": "ocw-content-storage",
    "OCW_UPLOAD_IMAGE_ONLY": "True",
    "OCW_LIVE_BUCKET": "ocw-content-live-production",
    "OLL_ALT_URL": "https://openlearninglibrary.mit.edu/courses",
    "OLL_API_ACCESS_TOKEN_URL": "https://openlearninglibrary.mit.edu/oauth2/access_token/",
    "OLL_API_URL": "https://discovery.openlearninglibrary.mit.edu/api/v1/catalogs/1/courses/",
    "OLL_BASE_URL": "https://openlearninglibrary.mit.edu/course/",
    "OLL_LEARNING_COURSE_BUCKET_NAME": "ol-data-lake-landing-zone-production",
    "OLL_LEARNING_COURSE_BUCKET_PREFIX": "open-learning-library/courses",
    "OPENSEARCH_DEFAULT_TIMEOUT": 30,
    "OPENSEARCH_INDEXING_CHUNK_SIZE": 75,
    "QDRANT_COLLECTION_NAME": f"mitlearn-{stack_info.env_suffix}",
    "QDRANT_DENSE_MODEL": "text-embedding-3-large",
    "QDRANT_ENCODER": "vector_search.encoders.litellm.LiteLLMEncoder",
    "CONTENT_FILE_EMBEDDING_CHUNK_OVERLAP": 51,
    "CONTENT_FILE_EMBEDDING_CHUNK_SIZE": 512,
    "EMBEDDING_SCHEDULE_MINUTES": 120,
    "PROLEARN_CATALOG_API_URL": "https://prolearn.mit.edu/graphql",
    "SEE_API_URL": "https://mit-unified-portal-prod-78eeds.43d8q2.usa-e2.cloudhub.io/api/",
    "SEE_API_ACCESS_TOKEN_URL": "https://mit-unified-portal-prod-78eeds.43d8q2.usa-e2.cloudhub.io/oauth/token",
    "SECURE_CROSS_ORIGIN_OPENER_POLICY": "None",
    "SEE_BASE_URL": "https://executive.mit.edu/",
    "SOCIAL_AUTH_OL_OIDC_KEY": "ol-mitlearn-client",
    "USE_X_FORWARDED_HOST": "True",
    "USE_X_FORWARDED_PORT": "True",
    "XPRO_CATALOG_API_URL": "https://xpro.mit.edu/api/programs/",
    "XPRO_COURSES_API_URL": "https://xpro.mit.edu/api/courses/",
    "XPRO_LEARNING_COURSE_BUCKET_NAME": "mitx-etl-xpro-production-mitxpro-production",
    "YOUTUBE_FETCH_SCHEDULE_SECONDS": 14400,
    "YOUTUBE_FETCH_TRANSCRIPT_SCHEDULE_SECONDS": 21600,
    "YOUTUBE_CONFIG_URL": "https://raw.githubusercontent.com/mitodl/open-video-data/mitopen/youtube/channels.yaml",
    "POSTHOG_ENABLED": "True",
    "POSTHOG_TIMEOUT_MS": 1000,
    "POSTHOG_PROJECT_ID": 63497,
}

# Values that require interpolation or other special considerations
interpolation_vars = mitlearn_config.get_object("interpolation_vars")

csrf_origins_list = interpolation_vars["csrf_domains"] or []
session_cookie_domain = interpolation_vars["session_cookie_domain"] or ""
cors_urls_list = interpolation_vars["cors_urls"] or []
cors_urls_json = json.dumps(cors_urls_list)
auth_allowed_redirect_hosts_list = (
    interpolation_vars["auth_allowed_redirect_hosts"] or []
)
auth_allowed_redirect_hosts_json = json.dumps(auth_allowed_redirect_hosts_list)

interpolated_vars = {
    "ACCESS_TOKEN_URL": f"https://{interpolation_vars['sso_url']}/realms/olapps/protocol/openid-connect/token",
    "AUTHORIZATION_URL": f"https://{interpolation_vars['sso_url']}/realms/olapps/protocol/openid-connect/auth",
    "CORS_ALLOWED_ORIGINS": cors_urls_json,
    "CSRF_TRUSTED_ORIGINS": json.dumps(csrf_origins_list),
    "KEYCLOAK_BASE_URL": f"https://{interpolation_vars['sso_url']}/",
    "MAILGUN_FROM_EMAIL": f"MIT Learn <no-reply@{interpolation_vars['mailgun_sender_domain']}>",
    "MAILGUN_SENDER_DOMAIN": interpolation_vars["mailgun_sender_domain"],
    "MAILGUN_URL": f"https://api.mailgun.net/v3/{interpolation_vars['mailgun_sender_domain']}",
    "MITOL_CORS_ORIGIN_WHITELIST": cors_urls_json,
    "OIDC_ENDPOINT": f"https://{interpolation_vars['sso_url']}/realms/olapps",
    "SESSION_COOKIE_DOMAIN": session_cookie_domain,
    "ALLOWED_REDIRECT_HOSTS": auth_allowed_redirect_hosts_json,
    "SOCIAL_AUTH_OL_OIDC_OIDC_ENDPOINT": f"https://{interpolation_vars['sso_url']}/realms/olapps",
    "USERINFO_URL": f"https://{interpolation_vars['sso_url']}/realms/olapps/protocol/openid-connect/userinfo",
}

# Combine two var sources above with values explictly defined in pulumi configuration
env_vars.update(**interpolated_vars)
env_vars.update(**mitlearn_config.get_object("vars"))

# Making these `get_secret_*()` calls children of the seemigly un-related vault mount `secret-mitopen/` tricks
# them into inheriting the correct vault provider rather than attempting to create their own (which won't work and / or
# will duplicate the existing vault provider)

# TODO @Ardiea: 01112024 We should be able to use vault.aws.get_access_credentials_output()
# for this but it doesn't seem to work
# auth_aws_mitx_creds_ol_mitopen_application = vault.aws.get_access_credentials_output(backend=mitlearn_vault_iam_role.backend, role=mitlearn_vault_iam_role.name, opts=InvokeOptions(parent=mitlearn_vault_iam_role))  # . TD003
secret_operations_global_embedly = vault.generic.get_secret_output(
    path="secret-operations/global/embedly",
    opts=InvokeOptions(parent=mitlearn_vault_iam_role),
)
secret_operations_global_odlbot_github_access_token = vault.generic.get_secret_output(
    path="secret-operations/global/odlbot-github-access-token",
    opts=InvokeOptions(parent=mitlearn_vault_iam_role),
)
secret_global_mailgun_api_key = vault.generic.get_secret_output(
    path="secret-global/mailgun",
    opts=InvokeOptions(parent=mitlearn_vault_iam_role),
)
secret_operations_global_mit_smtp = vault.generic.get_secret_output(
    path="secret-operations/global/mit-smtp",
    opts=InvokeOptions(parent=mitlearn_vault_iam_role),
)
secret_operations_global_update_search_data_webhook_key = (
    vault.generic.get_secret_output(
        path="secret-operations/global/update-search-data-webhook-key",
        opts=InvokeOptions(parent=mitlearn_vault_iam_role),
    )
)
secret_operations_sso_learn = vault.generic.get_secret_output(
    path="secret-operations/sso/mitlearn",
    opts=InvokeOptions(parent=mitlearn_vault_iam_role),
)
secret_operations_tika_access_token = vault.generic.get_secret_output(
    path="secret-operations/tika/access-token",
    opts=InvokeOptions(parent=mitlearn_vault_iam_role),
)

# Gets masked in any console outputs
sensitive_env_vars = {
    # Vars available locally from SOPS
    "CKEDITOR_ENVIRONMENT_ID": mitlearn_vault_secrets["ckeditor"]["environment_id"],
    "CKEDITOR_SECRET_KEY": mitlearn_vault_secrets["ckeditor"]["secret_key"],
    "CKEDITOR_UPLOAD_URL": mitlearn_vault_secrets["ckeditor"]["upload_url"],
    "EDX_API_CLIENT_ID": mitlearn_vault_secrets["edx_api_client"]["id"],
    "EDX_API_CLIENT_SECRET": mitlearn_vault_secrets["edx_api_client"]["secret"],
    "MITOL_JWT_SECRET": mitlearn_vault_secrets["jwt_secret"],
    "OLL_API_CLIENT_ID": mitlearn_vault_secrets["open_learning_library_client"][
        "client_id"
    ],
    "OLL_API_CLIENT_SECRET": mitlearn_vault_secrets["open_learning_library_client"][
        "client_secret"
    ],
    "OPENAI_API_KEY": mitlearn_vault_secrets["openai"]["api_key"],
    "OPENSEARCH_HTTP_AUTH": mitlearn_vault_secrets["opensearch"]["http_auth"],
    "QDRANT_API_KEY": mitlearn_vault_secrets["qdrant"]["api_key"],
    "QDRANT_HOST": mitlearn_vault_secrets["qdrant"]["host_url"],
    "SECRET_KEY": mitlearn_vault_secrets["django_secret_key"],
    "SENTRY_DSN": mitlearn_vault_secrets["sentry_dsn"],
    "STATUS_TOKEN": mitlearn_vault_secrets["django_status_token"],
    "YOUTUBE_DEVELOPER_KEY": mitlearn_vault_secrets["youtube_developer_key"],
    "POSTHOG_PROJECT_API_KEY": mitlearn_vault_secrets["posthog"]["project_api_key"],
    "POSTHOG_PERSONAL_API_KEY": mitlearn_vault_secrets["posthog"]["personal_api_key"],
    "SEE_API_CLIENT_ID": mitlearn_vault_secrets["see_api_client"]["id"],
    "SEE_API_CLIENT_SECRET": mitlearn_vault_secrets["see_api_client"]["secret"],
    "EMBEDLY_KEY": secret_operations_global_embedly.data.apply(
        lambda data: "{}".format(data["key"])
    ),
    "GITHUB_ACCESS_TOKEN": secret_operations_global_odlbot_github_access_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "MAILGUN_KEY": secret_global_mailgun_api_key.data.apply(
        lambda data: "{}".format(data["api_key"])
    ),
    "MITOL_EMAIL_HOST": secret_operations_global_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_host"])
    ),
    "MITOL_EMAIL_PASSWORD": secret_operations_global_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_password"])
    ),
    "MITOL_EMAIL_USER": secret_operations_global_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_username"])
    ),
    "OCW_NEXT_SEARCH_WEBHOOK_KEY": secret_operations_global_update_search_data_webhook_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "OCW_WEBHOOK_KEY": secret_operations_global_update_search_data_webhook_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "SOCIAL_AUTH_OL_OIDC_SECRET": secret_operations_sso_learn.data.apply(
        lambda data: "{}".format(data["client_secret"])
    ),
    "TIKA_ACCESS_TOKEN": secret_operations_tika_access_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
}

# There were a few undiscovered circular dependencies here that wasn't revealed until attempting
# to build the CI environment. Since we won't even need this in K8S we will just let it be
# for now behind this conditional


match stack_info.env_suffix:
    case "production":
        env_var_suffix = "PROD"
    case "qa":
        env_var_suffix = "RC"
    case "ci":
        env_var_suffix = "CI"
    case _:
        env_var_suffix = "INVALID"

mit_learn_posthog_proxy = mitlearn_config.require("posthog_proxy")

gh_repo = github.get_repository(
    full_name="mitodl/mit-learn", opts=InvokeOptions(provider=github_provider)
)


application_labels = k8s_global_labels | {
    "ol.mit.edu/application": "learn",
    "ol.mit.edu/pod-security-group": "learn",
}

learn_external_service_shared_plugins = OLApisixSharedPlugins(
    name="ol-mitlearn-external-service-apisix-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="mitlearn",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=learn_namespace,
        k8s_labels=application_labels,
        enable_defaults=True,
    ),
)

api_tls_secret_name = "api-mitlearn-tls-pair"  # pragma: allowlist secret # noqa: S105
cert_manager_certificate = OLCertManagerCert(
    f"ol-mitlearn-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="mitlearn",
        k8s_namespace=learn_namespace,
        k8s_labels=application_labels,
        create_apisixtls_resource=True,
        dest_secret_name=api_tls_secret_name,
        dns_names=[mitlearn_config.require("api_domain")],
    ),
)

xpro_consul_opts = get_consul_provider(
    stack_info=stack_info,
    consul_address=f"https://consul-xpro-{stack_info.env_suffix}.odl.mit.edu",
    provider_name=f"consul-provider-xpro-{stack_info.env_suffix}",
)
consul.Keys(
    f"learn-api-domain-consul-key-for-xpro-openedx-{stack_info.env_suffix}",
    keys=[
        consul.KeysKeyArgs(
            path="edxapp/learn-api-domain",
            delete=False,
            value=mitlearn_api_domain,
        ),
        consul.KeysKeyArgs(
            path="edxapp/learn-frontend-domain",
            delete=False,
            value=learn_frontend_domain,
        ),
    ],
    opts=xpro_consul_opts,
)


mitxonline_consul_opts = get_consul_provider(
    stack_info,
    consul_address=f"https://consul-mitxonline-{stack_info.env_suffix}.odl.mit.edu",
    provider_name=f"consul-provider-mitxonline-{stack_info.env_suffix}",
)
consul.Keys(
    "learn-api-domain-consul-key-for-mitxonline-openedx",
    keys=[
        consul.KeysKeyArgs(
            path="edxapp/learn-api-domain",
            delete=False,
            value=mitlearn_api_domain,
        ),
        consul.KeysKeyArgs(
            path="edxapp/learn-frontend-domain",
            delete=False,
            value=learn_frontend_domain,
        ),
    ],
    opts=mitxonline_consul_opts,
)

mitx_consul_opts = get_consul_provider(
    stack_info=stack_info,
    consul_address=f"https://consul-mitx-{stack_info.env_suffix}.odl.mit.edu",
    provider_name=f"consul-provider-mitx-{stack_info.env_suffix}",
)
consul.Keys(
    f"learn-api-domain-consul-key-for-mitx-openedx-{stack_info.env_suffix}",
    keys=[
        consul.KeysKeyArgs(
            path="edxapp/learn-api-domain",
            delete=False,
            value=mitlearn_api_domain,
        ),
        consul.KeysKeyArgs(
            path="edxapp/learn-frontend-domain",
            delete=False,
            value=learn_frontend_domain,
        ),
    ],
    opts=mitx_consul_opts,
)

mitx_staging_consul_opts = get_consul_provider(
    stack_info=stack_info,
    consul_address=f"https://consul-mitx-staging-{stack_info.env_suffix}.odl.mit.edu",
    provider_name=f"consul-provider-mitx-staging-{stack_info.env_suffix}",
)
consul.Keys(
    f"learn-api-domain-consul-key-for-mitx-staging-openedx-{stack_info.env_suffix}",
    keys=[
        consul.KeysKeyArgs(
            path="edxapp/learn-api-domain",
            delete=False,
            value=mitlearn_api_domain,
        ),
        consul.KeysKeyArgs(
            path="edxapp/learn-frontend-domain",
            delete=False,
            value=learn_frontend_domain,
        ),
    ],
    opts=mitx_staging_consul_opts,
)


# Redis / Elasticache
# only for applications deployed in k8s
redis_config = Config("redis")
redis_defaults = defaults(stack_info)["redis"]
instance_type = redis_config.get("instance_type") or redis_defaults["instance_type"]
redis_defaults["instance_type"] = instance_type
redis_cluster_security_group = ec2.SecurityGroup(
    f"ol-mitlearn-redis-cluster-security-group-{stack_info.env_suffix}",
    name_prefix=f"ol-mitlearn-redis-cluster-security-group-{stack_info.env_suffix}",
    description="Access control for the mitlearn redis cluster.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                mitlearn_app_security_group.id,
                operations_vpc["security_groups"]["celery_monitoring"],
                cluster_substructure_stack.require_output(
                    "cluster_keda_security_group_id"
                ),
            ],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description="Allow application pods to talk to Redis",
        ),
    ],
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
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
    cluster_description="Redis cluster for MIT Learn",
    cluster_name=f"mitlearn-redis-{stack_info.env_suffix}",
    subnet_group=apps_vpc["elasticache_subnet"],
    security_groups=[redis_cluster_security_group.id],
    tags=aws_config.tags,
    **redis_defaults,
)
redis_cache = OLAmazonCache(
    redis_cache_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"mitlearn-redis-{stack_info.env_suffix}-redis-elasticache-cluster"
            )
        ]
    ),
)

# Create all Kubernetes secrets needed by the application
secret_names, secret_resources = create_mitlearn_k8s_secrets(
    stack_info=stack_info,
    mitlearn_namespace=learn_namespace,
    k8s_global_labels=k8s_global_labels,
    vault_k8s_resources=vault_k8s_resources,
    mitlearn_vault_mount=mitlearn_vault_mount,
    db_config=mitlearn_vault_backend,  # Use the original DB config object
    redis_password=redis_config.require("password"),
    redis_cache=redis_cache,
)

if "MIT_LEARN_DOCKER_TAG" not in os.environ:
    msg = "MIT_LEARN_DOCKER_TAG must be set."
    raise OSError(msg)
MIT_LEARN_DOCKER_TAG = os.environ["MIT_LEARN_DOCKER_TAG"]

# Configure and deploy the mitlearn application using OLApplicationK8s
if mitlearn_config.get_bool("use_granian"):
    cmd_array = [
        "granian",
    ]
    arg_array = [
        "--interface",
        "wsgi",
        "--host",
        "0.0.0.0",  # noqa: S104
        "--port",
        f"{DEFAULT_WSGI_PORT}",
        "--workers",
        "1",
        "--log-level",
        "warning",
        "main.wsgi:application",
    ]
    nginx_config_path = "files/web.conf_granian"
else:
    cmd_array = ["uwsgi"]
    arg_array = ["/tmp/uwsgi.ini"]  # noqa: S108
    nginx_config_path = "files/web.conf_uwsgi"

mitlearn_k8s_app = OLApplicationK8s(
    ol_app_k8s_config=OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=env_vars,
        application_name="mitlearn",
        application_namespace=learn_namespace,
        application_lb_service_name="mitlearn-webapp",
        application_lb_service_port_name="http",
        k8s_global_labels=k8s_global_labels,
        # Reference all Kubernetes secrets containing environment variables
        env_from_secret_names=secret_names,
        application_min_replicas=mitlearn_config.get_int("min_replicas") or 2,
        application_max_replicas=mitlearn_config.get_int("max_replicas") or 10,
        application_security_group_id=mitlearn_app_security_group.id,
        application_security_group_name=mitlearn_app_security_group.name,
        application_image_repository="mitodl/mit-learn-app",
        application_docker_tag=MIT_LEARN_DOCKER_TAG,
        application_cmd_array=cmd_array,
        application_arg_array=arg_array,
        vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
        import_nginx_config=True,  # Assuming Django app needs nginx
        import_nginx_config_path=nginx_config_path,
        import_uwsgi_config=True,
        init_migrations=False,
        init_collectstatic=True,  # Assuming Django app needs collectstatic
        pre_deploy_commands=[("migrate", ["scripts/heroku-release-phase.sh"])],
        celery_worker_configs=[
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="default",
                max_replicas=20,
                redis_host=redis_cache.address,
                redis_password=redis_config.require("password"),
            ),
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="edx_content",
                redis_host=redis_cache.address,
                redis_password=redis_config.require("password"),
            ),
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="embeddings",
                max_replicas=30,
                redis_host=redis_cache.address,
                redis_password=redis_config.require("password"),
            ),
        ],
        resource_requests={"cpu": "500m", "memory": "1600Mi"},
        resource_limits={"cpu": "1000m", "memory": "2000Mi"},
        hpa_scaling_metrics=[
            kubernetes.autoscaling.v2.MetricSpecArgs(
                type="Resource",
                resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                    name="cpu",
                    target=kubernetes.autoscaling.v2.MetricTargetArgs(
                        type="Utilization",
                        average_utilization=60,  # Target CPU utilization (60%)
                    ),
                ),
            ),
            # Scale up when avg usage exceeds: 1800 * 0.8 = 1440 Mi
            kubernetes.autoscaling.v2.MetricSpecArgs(
                type="Resource",
                resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                    name="memory",
                    target=kubernetes.autoscaling.v2.MetricTargetArgs(
                        type="Utilization",
                        average_utilization=80,  # Target memory utilization (80%)
                    ),
                ),
            ),
        ],
    ),
    opts=ResourceOptions(
        depends_on=[
            mitlearn_app_security_group,
            *secret_resources,
        ]
    ),
)

mitlearn_k8s_app_oidc_resources_no_prefix = OLApisixOIDCResources(
    f"ol-mitlearn-k8s-olapisixoidcresources-no-prefix-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="mitlearn-k8s-no-prefix",
        k8s_labels=application_labels,
        k8s_namespace=learn_namespace,
        oidc_logout_path="/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{mitlearn_config.get('api_domain')}/logout/",
        oidc_session_cookie_lifetime=60 * 20160,
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/mitlearn",
        vaultauth=vault_k8s_resources.auth_name,
    ),
)
mitlearn_k8s_app_oidc_resources = OLApisixOIDCResources(
    f"ol-mitlearn-k8s-olapisixoidcresources-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="mitlearn-k8s",
        k8s_labels=application_labels,
        k8s_namespace=learn_namespace,
        oidc_logout_path="/learn/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{mitlearn_config.get('api_domain')}/learn/logout/",
        oidc_session_cookie_lifetime=60 * 20160,
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/mitlearn",
        vaultauth=vault_k8s_resources.auth_name,
    ),
)

proxy_rewrite_plugin_config = OLApisixPluginConfig(
    name="proxy-rewrite",
    config={
        "regex_uri": [
            "/learn/(.*)",
            "/$1",
        ],
    },
)

learn_external_service_apisix_route_no_prefix = OLApisixRoute(
    name=f"ol-mitlearn-k8s-apisix-route-no-prefix-{stack_info.env_suffix}",
    k8s_namespace=learn_namespace,
    k8s_labels=application_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="passauth",
            priority=0,
            shared_plugin_config_name=learn_external_service_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin_config,
                mitlearn_k8s_app_oidc_resources_no_prefix.get_full_oidc_plugin_config(
                    unauth_action="pass"
                ),
            ],
            hosts=[mitlearn_api_domain],
            paths=["/*"],
            backend_service_name=mitlearn_k8s_app.application_lb_service_name,
            backend_service_port=mitlearn_k8s_app.application_lb_service_port_name,
        ),
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            shared_plugin_config_name=learn_external_service_shared_plugins.resource_name,
            plugins=[
                OLApisixPluginConfig(name="redirect", config={"uri": "/logout/oidc"}),
            ],
            hosts=[mitlearn_api_domain],
            paths=["/logout/oidc/*"],
            backend_service_name=mitlearn_k8s_app.application_lb_service_name,
            backend_service_port=mitlearn_k8s_app.application_lb_service_port_name,
        ),
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            shared_plugin_config_name=learn_external_service_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin_config,
                mitlearn_k8s_app_oidc_resources_no_prefix.get_full_oidc_plugin_config(
                    unauth_action="auth"
                ),
            ],
            hosts=[mitlearn_api_domain],
            paths=[
                "/admin/login/*",
                "/login",
                "/login/*",
            ],
            backend_service_name=mitlearn_k8s_app.application_lb_service_name,
            backend_service_port=mitlearn_k8s_app.application_lb_service_port_name,
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

learn_external_service_apisix_route = OLApisixRoute(
    name=f"ol-mitlearn-k8s-apisix-route-{stack_info.env_suffix}",
    k8s_namespace=learn_namespace,
    k8s_labels=application_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="passauth",
            priority=0,
            shared_plugin_config_name=learn_external_service_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin_config,
                mitlearn_k8s_app_oidc_resources.get_full_oidc_plugin_config(
                    unauth_action="pass"
                ),
            ],
            hosts=[mitlearn_api_domain],
            paths=["/learn/*"],
            backend_service_name=mitlearn_k8s_app.application_lb_service_name,
            backend_service_port=mitlearn_k8s_app.application_lb_service_port_name,
        ),
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            shared_plugin_config_name=learn_external_service_shared_plugins.resource_name,
            plugins=[
                OLApisixPluginConfig(name="redirect", config={"uri": "/logout/oidc"}),
            ],
            hosts=[mitlearn_api_domain],
            paths=["/learn/logout/oidc/*"],
            backend_service_name=mitlearn_k8s_app.application_lb_service_name,
            backend_service_port=mitlearn_k8s_app.application_lb_service_port_name,
        ),
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            shared_plugin_config_name=learn_external_service_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin_config,
                mitlearn_k8s_app_oidc_resources.get_full_oidc_plugin_config(
                    unauth_action="auth"
                ),
            ],
            hosts=[mitlearn_api_domain],
            paths=[
                "/learn/admin/login/*",
                "/learn/login",
                "/learn/login/*",
            ],
            backend_service_name=mitlearn_k8s_app.application_lb_service_name,
            backend_service_port=mitlearn_k8s_app.application_lb_service_port_name,
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)


export(
    "mit_learn",
    {
        "redis": redis_cache.address,
        "redis_token": redis_cache.cache_cluster.auth_token,
        "iam_policy": mitlearn_iam_policy.arn,
        "vault_iam_role": Output.all(
            mitlearn_vault_iam_role.backend, mitlearn_vault_iam_role.name
        ).apply(lambda role: f"{role[0]}/roles/{role[1]}"),
    },
)
