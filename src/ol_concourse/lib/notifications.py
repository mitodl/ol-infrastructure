from typing import Optional

from ol_concourse.lib.models.pipeline import PutStep, Resource


def notification(
    resource: Resource,
    title: str,
    body: str,
    alert_type: Optional[str] = "default",
) -> PutStep:
    """Generate a PutStep for sending notifications (to just slack, for now).

    :param resource: The slack_notification_resource object for the pipeline.
        See src/ol_concourse/lib/resources.py
    :param title: The text to send to slack as the 'title' of the notification. The bold
        main line.
    :param body: The text to send to slack as the 'body' of the notification. Additional
        details that are hidden when the alert is collapsed.
    :param alert_type: The type of alert to send. Determined the color of the alert
        in slack.
        See: https://github.com/arbourd/concourse-slack-alert-resource#alert-types.
        At this time we don't support 'started' notifications.

    :returns: A `PutStep` object that can be executed as an 'on_success',
        'on_failure', etc
    """
    params = {"alert_type": alert_type, "message": title, "text": body}
    return PutStep(
        put=resource.name,
        params=params,
    )
