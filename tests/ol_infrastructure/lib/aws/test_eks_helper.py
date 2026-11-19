"""Node user data shared by the EKS node groups and Karpenter nodes."""

from ol_infrastructure.lib.aws.eks_helper import DISABLE_SCTP_USER_DATA


def test_sctp_user_data_is_a_shell_script_that_blocks_the_module():
    # pulumi-eks node_user_data must start with an interpreter directive.
    assert DISABLE_SCTP_USER_DATA.startswith("#!/bin/bash\n")
    assert (
        'echo "install sctp /bin/false" > /etc/modprobe.d/disable-sctp.conf'
        in DISABLE_SCTP_USER_DATA
    )
