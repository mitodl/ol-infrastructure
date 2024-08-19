import pytest
from pydantic import ValidationError

from ol_infrastructure.components.aws.olvpc import OLVPCConfig

VALID_CONFIG = {
    "tags": {"OU": "operations", "Environment": "test"},
    "vpc_name": "test",
    "cidr_block": "192.168.0.0/16",
    "num_subnets": 3,
}


def test_min_subnets_validation():
    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["num_subnets"] = 1
        OLVPCConfig(**bad_config)


@pytest.mark.parametrize(
    "subnet",
    [
        "1.2.3.4/5",
        "10.0.0.0/32",
    ],
)
def test_bad_subnet_validation(subnet):
    with pytest.raises(ValidationError):  # noqa: PT012
        bad_config = VALID_CONFIG.copy()
        bad_config["cidr_block"] = subnet
        OLVPCConfig(**bad_config)
