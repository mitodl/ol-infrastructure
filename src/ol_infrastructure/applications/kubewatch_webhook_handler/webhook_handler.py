"""
Custom webhook handler for kubewatch that enriches deployment notifications.

This service receives webhook events from kubewatch and:
1. Extracts deployment information including labels, start time, and status
2. Formats rich Slack messages with all deployment details
3. Filters to only namespaces with OLApplicationK8s deployments
4. Posts to Slack with enhanced formatting
"""

import json
import logging
import os
from http import HTTPStatus
from typing import Any

import requests
from flask import Flask, jsonify, request
from kubernetes import client, config

# Constants
MAX_IMAGE_LENGTH = 50
MAX_SLACK_FIELDS = 10
MAX_LABELS_DISPLAYED = 5
SLACK_FIELD_BUFFER = 9  # Leave room for labels field

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Load Kubernetes configuration
try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes configuration")
except config.ConfigException:
    config.load_kube_config()
    logger.info("Loaded kubeconfig from file")

# Initialize Kubernetes API clients
apps_v1 = client.AppsV1Api()

# Configuration from environment variables
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
WATCHED_NAMESPACES = os.environ.get("WATCHED_NAMESPACES", "").split(",")
WATCHED_NAMESPACES = [ns.strip() for ns in WATCHED_NAMESPACES if ns.strip()]

logger.info("Watching namespaces: %s", WATCHED_NAMESPACES)


def get_deployment_details(namespace: str, name: str) -> dict[str, Any] | None:
    """Fetch detailed deployment information from Kubernetes API."""
    try:
        deployment = apps_v1.read_namespaced_deployment(name, namespace)

        # Extract deployment metadata
        labels = deployment.metadata.labels or {}
        annotations = deployment.metadata.annotations or {}

        # Get status information
        status = deployment.status
        conditions = status.conditions or []

        # Find progressing and available conditions
        progressing_condition = next(
            (c for c in conditions if c.type == "Progressing"), None
        )
        available_condition = next(
            (c for c in conditions if c.type == "Available"), None
        )

        # Calculate rollout status
        rollout_status = "Unknown"
        if progressing_condition:
            if progressing_condition.reason == "NewReplicaSetAvailable":
                rollout_status = "✅ Successfully Deployed"
            elif progressing_condition.reason == "ProgressDeadlineExceeded":
                rollout_status = "❌ Deployment Failed"
            elif progressing_condition.reason == "ReplicaSetUpdated":
                rollout_status = "🔄 Rolling Out"

        # Get timestamps
        creation_time = deployment.metadata.creation_timestamp
        last_update_time = None
        if progressing_condition and progressing_condition.last_update_time:
            last_update_time = progressing_condition.last_update_time

        # Get replica information
        desired_replicas = deployment.spec.replicas or 0
        ready_replicas = status.ready_replicas or 0
        updated_replicas = status.updated_replicas or 0

        return {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "annotations": annotations,
            "creation_time": creation_time,
            "last_update_time": last_update_time,
            "rollout_status": rollout_status,
            "desired_replicas": desired_replicas,
            "ready_replicas": ready_replicas,
            "updated_replicas": updated_replicas,
            "image": deployment.spec.template.spec.containers[0].image
            if deployment.spec.template.spec.containers
            else "Unknown",
            "progressing_message": progressing_condition.message
            if progressing_condition
            else "No status available",
            "is_available": available_condition.status == "True"
            if available_condition
            else False,
        }
    except client.exceptions.ApiException:
        logger.exception("Error fetching deployment %s/%s", namespace, name)
        return None


def format_slack_message(
    event_data: dict[str, Any], deployment_details: dict[str, Any] | None
) -> dict[str, Any]:
    """Format a rich Slack message with deployment details."""

    event_type = event_data.get("eventType", "unknown")
    namespace = event_data.get("namespace", "unknown")
    name = event_data.get("name", "unknown")

    # Event type emoji mapping
    event_emoji = {
        "create": "🆕",
        "update": "🔄",
        "delete": "🗑️",
    }
    emoji = event_emoji.get(event_type.lower(), "📝")

    # Base message
    title = f"{emoji} Deployment {event_type.title()}: {namespace}/{name}"

    if not deployment_details:
        # Fallback for when we can't get details
        return {
            "text": title,
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": title,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Event:* {event_type}\n"
                            f"*Namespace:* {namespace}\n"
                            f"*Deployment:* {name}"
                        ),
                    },
                },
            ],
        }

    # Rich message with deployment details
    fields = []

    # Status and replicas
    fields.append(
        {
            "type": "mrkdwn",
            "text": f"*Status:*\n{deployment_details['rollout_status']}",
        }
    )
    fields.append(
        {
            "type": "mrkdwn",
            "text": (
                f"*Replicas:*\n{deployment_details['ready_replicas']}/"
                f"{deployment_details['desired_replicas']} ready"
            ),
        }
    )

    # Timestamps
    if deployment_details["creation_time"]:
        created = deployment_details["creation_time"].strftime("%Y-%m-%d %H:%M:%S UTC")
        fields.append({"type": "mrkdwn", "text": f"*Created:*\n{created}"})

    if deployment_details["last_update_time"]:
        updated = deployment_details["last_update_time"].strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        fields.append({"type": "mrkdwn", "text": f"*Last Update:*\n{updated}"})

    # Image
    image = deployment_details["image"]
    if len(image) > MAX_IMAGE_LENGTH:
        # Shorten long image names
        image_parts = image.split("/")
        image = f".../{'/'.join(image_parts[-2:])}"

    # Only add image if we haven't hit the field limit
    if len(fields) < MAX_SLACK_FIELDS:
        fields.append({"type": "mrkdwn", "text": f"*Image:*\n`{image}`"})

    # Important labels - limit to avoid exceeding fields total
    labels = deployment_details["labels"]
    important_labels = {
        k: v
        for k, v in labels.items()
        if k.startswith("ol.mit.edu/") or k in ["app", "version"]
    }
    # Leave room for at least one label field
    if important_labels and len(fields) < SLACK_FIELD_BUFFER:
        # Limit to MAX_LABELS_DISPLAYED labels
        label_items = sorted(important_labels.items())[:MAX_LABELS_DISPLAYED]
        label_text = "\n".join([f"• `{k}`: {v}" for k, v in label_items])
        fields.append({"type": "mrkdwn", "text": f"*Labels:*\n{label_text}"})

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": title,
            },
        },
        {"type": "section", "fields": fields},
    ]

    # Add status message if available
    if deployment_details["progressing_message"]:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_{deployment_details['progressing_message']}_",
                    }
                ],
            }
        )

    return {
        "text": title,  # Fallback text for notifications
        "blocks": blocks,
    }


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy"}), 200


@app.route("/webhook/kubewatch", methods=["POST"])
def webhook_handler():
    """Handle webhook events from kubewatch."""
    try:
        # Parse the incoming event
        event_data = request.get_json()
        logger.info("Received event: %s", json.dumps(event_data, default=str))

        # Extract event information from eventmeta
        event_meta = event_data.get("eventmeta", {})
        namespace = event_meta.get("namespace", "")
        name = event_meta.get("name", "")
        kind = event_meta.get("kind", "")
        event_type = event_meta.get(
            "reason", ""
        )  # kubewatch uses "reason" not "eventType"

        # Filter by namespace if configured
        if WATCHED_NAMESPACES and namespace not in WATCHED_NAMESPACES:
            logger.info("Ignoring event from unwatched namespace: %s", namespace)
            return jsonify(
                {"status": "ignored", "reason": "namespace not watched"}
            ), HTTPStatus.OK

        # Only process deployment events
        if kind.lower() != "deployment":
            logger.info("Ignoring non-deployment event: %s", kind)
            return (
                jsonify({"status": "ignored", "reason": "not a deployment"}),
                HTTPStatus.OK,
            )

        # Get detailed deployment information
        deployment_details = None
        if event_type.lower() != "deleted":
            deployment_details = get_deployment_details(namespace, name)

        # Create event data in expected format for format_slack_message
        formatted_event_data = {
            "namespace": namespace,
            "name": name,
            "kind": kind,
            "eventType": event_type,
        }

        # Format the Slack message
        slack_message = format_slack_message(formatted_event_data, deployment_details)

        # Log the message being sent for debugging
        message_preview = json.dumps(slack_message, default=str)[:500]
        logger.info("Sending Slack message: %s", message_preview)

        # Send to Slack
        if SLACK_WEBHOOK_URL:
            response = requests.post(
                SLACK_WEBHOOK_URL,
                json=slack_message,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if response.status_code != HTTPStatus.OK:
                logger.error(
                    "Slack API error: %s - %s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            logger.info(
                "Successfully sent notification to Slack for %s/%s (status: %s)",
                namespace,
                name,
                response.status_code,
            )
        else:
            logger.warning(
                "SLACK_WEBHOOK_URL not configured, skipping Slack notification"
            )

        return jsonify({"status": "success"}), HTTPStatus.OK

    except Exception:
        logger.exception("Error processing webhook")
        return (
            jsonify({"status": "error", "message": "Internal server error"}),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


if __name__ == "__main__":
    # Run the Flask app
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)  # noqa: S104
