"""Grafana credential for the release markers the k8s app deploy jobs post.

The Production deploy jobs in ``ol_concourse/pipelines/infrastructure/k8s_apps``
post an organization annotation for each release (see
``scripts/grafana_release_marker.py`` there). They authenticate as this
service account, which has no basic role (``None``) and only the two fixed
roles in ``FIXED_ROLES``.

The token goes to ``secret-concourse/infrastructure/grafana_annotations``,
where those pipelines read it as ``((grafana_annotations.token))``.

Production stack only: the deploy jobs run on the production Concourse, which
reads the production Vault, and only the production Grafana holds the
production clusters' metrics and logs.
"""

import pulumi_vault as vault
import pulumiverse_grafana as grafana
from pulumi import InvokeOptions, Output, ResourceOptions

SERVICE_ACCOUNT_NAME = "concourse-release-markers"
CONCOURSE_VAULT_PATH = "secret-concourse/infrastructure/grafana_annotations"
# The marker script reads the last marker before it posts one. The writer role
# looks like it would cover both but grants create, delete and write only;
# ``annotations:read`` comes from the reader role.
FIXED_ROLES = ("fixed:annotations:reader", "fixed:annotations:writer")


def create(grafana_url: str, resource_opts: ResourceOptions) -> None:
    """Create the service account and hand its token to Concourse via Vault."""
    service_account = grafana.oss.ServiceAccount(
        SERVICE_ACCOUNT_NAME,
        name=SERVICE_ACCOUNT_NAME,
        role="None",
        opts=resource_opts,
    )
    for role_name in FIXED_ROLES:
        role = grafana.enterprise.get_role_output(
            name=role_name,
            opts=InvokeOptions(provider=resource_opts.provider),
        )
        grafana.enterprise.RoleAssignmentItem(
            f"{SERVICE_ACCOUNT_NAME}-{role_name.split(':', 1)[1].replace(':', '-')}",
            role_uid=role.uid,
            service_account_id=service_account.id,
            opts=resource_opts,
        )
    token = grafana.oss.ServiceAccountToken(
        f"{SERVICE_ACCOUNT_NAME}-token",
        name=SERVICE_ACCOUNT_NAME,
        service_account_id=service_account.id,
        opts=resource_opts,
    )
    vault.generic.Secret(
        f"{SERVICE_ACCOUNT_NAME}-vault-secret",
        path=CONCOURSE_VAULT_PATH,
        data_json=Output.json_dumps({"url": grafana_url, "token": token.key}),
    )
