from enum import Enum, unique

from pydantic import BaseModel, field_validator

from ol_infrastructure.lib.aws.ec2_helper import aws_regions
from ol_infrastructure.lib.pulumi_helper import StackInfo

REQUIRED_TAGS = {"OU", "Environment"}
RECOMMENDED_TAGS = {"Application", "Owner"}


@unique
class BusinessUnit(str, Enum):
    """Canonical source of truth for defining valid OU tags.

    We rely on tagging AWS resources with a valid OU to allow for cost allocation to
    different business units.
    """

    bootcamps = "bootcamps"
    data = "data"
    digital_credentials = "digital-credentials"
    ecommerce = "unified-ecommerce"
    micromasters = "micromasters"
    mit_learn = "mit-learn"
    mit_open = "mit-open"
    mitx = "mitx"
    mitx_online = "mitxonline"
    ocw = "open-courseware"
    operations = "operations"
    ovs = "odl-video"
    residential = "residential"
    residential_staging = "residential-staging"
    xpro = "mitxpro"


@unique
class Environment(str, Enum):
    """Canonical reference for valid environment names."""

    applications = "applications"
    data = "data"
    mitx = "mitx"
    mitx_online = "mitxonline"
    mitx_staging = "mitx-staging"
    operations = "operations"
    xpro = "xpro"


@unique
class Services(str, Enum):
    """Canonical source of truth for defining apps."""

    airbyte = "airbyte"
    superset = "superset"
    bootcamps = "bootcamps"
    botkube = "botkube"
    dagster = "dagster"
    ecommerce = "unified-ecommerce"
    edxapp = "edxapp"
    keycloak = "keycloak"
    kubewatch = "kubewatch"
    jupyterhub = "jupyterhub"
    micromasters = "micromasters"
    mit_learn = "mit-learn"
    mit_open = "open"
    mitx_edx = "mitx-edx"
    mitxonline = "mitxonline"
    mitxonline_edx = "mitxonline-edx"
    mitxpro_edx = "xpro-edx"
    ocw_build = "ocw-build"
    odl_video_service = "ovs"
    open_metadata = "open-metadata"
    redash = "redash"
    xpro = "xpro"


class K8sGlobalLabels(BaseModel):
    """Base class for Kubernetes resource labels."""

    ou: BusinessUnit
    service: Services
    stack: StackInfo

    def model_dump(self, *args, **kwargs):
        kwargs["exclude_none"] = True

        model_dict = super().model_dump(*args, **kwargs)
        new_dict = {}
        for key in model_dict:
            new_dict[f"ol.mit.edu/{key}"] = model_dict[key]
        new_dict["ol.mit.edu/stack"] = self.stack.full_name
        new_dict["ol.mit.edu/environment"] = self.stack.env_suffix
        new_dict["ol.mit.edu/application"] = self.stack.env_prefix
        return new_dict


class AWSBase(BaseModel):
    """Base class for configuration objects to pass to AWS component resources."""

    tags: dict[str, str]
    region: str = "us-east-1"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tags.update({"pulumi_managed": "true"})

    @field_validator("tags")
    @classmethod
    def enforce_tags(cls, tags: dict[str, str]) -> dict[str, str]:
        if not REQUIRED_TAGS.issubset(tags.keys()):
            msg = f"Not all required tags have been specified. Missing tags: {REQUIRED_TAGS.difference(tags.keys())}"  # noqa: E501
            raise ValueError(msg)
        try:
            BusinessUnit(tags["OU"])
        except ValueError as exc:
            msg = "The OU tag specified is not a valid business unit"
            raise ValueError(msg) from exc
        return tags

    @field_validator("region")
    @classmethod
    def check_region(cls, region: str) -> str:
        if region not in aws_regions():
            msg = "The specified region does not exist"
            raise ValueError(msg)
        return region

    def merged_tags(self, *new_tags: dict[str, str]) -> dict[str, str]:
        """Return a dictionary of existing tags with the ones passed in.

        This generates a new dictionary of tags in order to allow for a broadly
        applicable set of tags to then be updated with specific tags to be set on child
        resources in a ComponentResource class.

        :param *new_tags: One or more dictionaries of specific tags to be set on
                            a child resource.
        :type new_tags: Dict[Text, Text]

        :returns: Merged dictionary of base tags and specific tags to be set on a child
                  resource.

        :rtype: Dict[Text, Text]
        """
        tag_dict = self.tags.copy()
        for tags in new_tags:
            tag_dict.update(tags)
        return tag_dict
