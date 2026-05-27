"""Airbyte metadata ingestion workflow."""

import os

from metadata.workflow.metadata import MetadataWorkflow

config = {
    "source": {
        "type": "airbyte",
        "serviceName": os.environ["OM_SERVICE_NAME"],
        "serviceConnection": {
            "config": {
                "type": "Airbyte",
                "hostPort": os.environ["OM_AIRBYTE_HOST_PORT"],
                "apiVersion": "api/public/v1",
            }
        },
        "sourceConfig": {"config": {"type": "PipelineMetadata"}},
    },
    "sink": {"type": "metadata-rest", "config": {}},
    "workflowConfig": {
        "openMetadataServerConfig": {
            "hostPort": os.environ["OM_SERVER_URL"],
            "authProvider": "openmetadata",
            "securityConfig": {"jwtToken": os.environ["OM_BOT_JWT_TOKEN"]},
        }
    },
}
workflow = MetadataWorkflow.create(config)
workflow.execute()
workflow.raise_from_status()
