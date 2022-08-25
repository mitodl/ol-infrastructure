from typing import Optional

from pydantic import BaseModel, Field, validator

from concourse.lib.models.pipeline import Job, Resource, ResourceType


class PipelineFragment(BaseModel):
    resource_types: list[Optional[ResourceType]] = Field(default_factory=list)
    resources: list[Optional[Resource]] = Field(default_factory=list)
    jobs: list[Optional[Job]] = Field(default_factory=list)

    @validator("resource_types")
    def deduplicate_resource_types(cls, resource_types):
        unique_resource_types = []
        resource_type_identifiers = set()
        for resource_type in resource_types:
            if resource_type.name not in resource_type_identifiers:
                resource_type_identifiers.add(resource_type.name)
                unique_resource_types.append(resource_type)
        return unique_resource_types

    @validator("resources")
    def deduplicate_resources(cls, resources):
        unique_resources = []
        resource_identifiers = set()
        for resource in resources:
            if resource.name not in resource_identifiers:
                resource_identifiers.add(resource.name)
                unique_resources.append(resource)
        return unique_resources
