"""
This creates a single approle auth backend at auth/approle.

The names of the roles under that backend are based on the hcl
file names in the vault/policies folder.
"""

from pathlib import Path, PurePath

from pulumi import Config, export
from pulumi_vault import AuthBackend, Policy, approle

from ol_infrastructure.lib.pulumi_helper import parse_stack

env_config = Config("environment")
stack_info = parse_stack()
constituent_approle_export = {}

approle_backend = AuthBackend(
    "approle",
    description=f"appRole backend for {stack_info.env_suffix}",
    path="approle",
    type="approle",
)

policy_folder = sorted(
    (Path(__file__).resolve().parent)
    .parent.joinpath("policies/approle/")
    .rglob("*.hcl")
)
for hcl_file in policy_folder:
    constituent_name = PurePath(hcl_file).stem
    policy_file = open(hcl_file).read()  # noqa: PTH123
    constituent_policy = Policy(constituent_name, policy=policy_file)
    constituent_approle = approle.AuthBackendRole(
        f"approle-{constituent_name}",
        backend="approle",
        role_name=constituent_name,
        token_policies=[constituent_policy],
    )
    constituent_approle_export.update(
        {
            f"{constituent_name}-{stack_info.env_suffix}": constituent_approle.id,
        }
    )

export("constituent_approles", constituent_approle_export)
