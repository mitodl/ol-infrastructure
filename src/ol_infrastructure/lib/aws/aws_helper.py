from functools import lru_cache

import pulumi_aws as aws


@lru_cache
def aws_account_id() -> str:
    """Look up the account ID of the calling AWS identity.

    A function rather than a module constant so that importing this
    module does not call AWS. Programs that render for a cluster
    outside AWS import it through the Kubernetes components.

    :returns: The AWS account ID
    :rtype: str
    """
    return aws.get_caller_identity().account_id
