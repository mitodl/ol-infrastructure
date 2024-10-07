from typing import Literal, Optional, Union

import pulumi
import pulumi_aws as aws
from pydantic import ConfigDict, PositiveInt

from ol_infrastructure.lib.aws.iam_helper import oidc_trust_policy_template
from ol_infrastructure.lib.ol_types import AWSBase


class OLEKSTrustRoleConfig(AWSBase):
    account_id: Union[str, PositiveInt]
    cluster_name: str
    cluster_identities: pulumi.Output
    description: str
    policy_operator: Literal["StringEquals", "StringLike"]
    role_name: str
    service_account_identifier: str
    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLEKSTrustRole(pulumi.ComponentResource):
    """Component resource to create an IAM Trust Role that can be associated with a K8S
    service account
    """

    role: aws.iam.Role = None

    def __init__(
        self,
        name: str,
        role_config: OLEKSTrustRoleConfig,
        opts: Optional[pulumi.ResourceOptions] = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:eks:OLEKSTrustRole",
            name,
            None,
            opts,
        )

        self.role = aws.iam.Role(
            f"{role_config.cluster_name}-{role_config.role_name}-trust-role",
            name=f"{role_config.cluster_name}-{role_config.role_name}-trust-role",
            path=f"/ol-infrastructure/eks/{role_config.cluster_name}/",
            assume_role_policy=role_config.cluster_identities.apply(
                lambda ids: oidc_trust_policy_template(
                    oidc_identifier=ids[0]["oidcs"][0]["issuer"],
                    account_id=str(role_config.account_id),
                    k8s_service_account_identifier=role_config.service_account_identifier,
                    operator=role_config.policy_operator,
                )
            ),
            description=role_config.description,
            tags=role_config.tags,
            opts=pulumi.ResourceOptions(parent=self).merge(opts),
        )
