from typing import Optional

from concourse.lib.models.pipeline import PutStep, Resource


def notification(
    resource: Resource,
    title: str,
    body: str,
    alert_type: Optional[str] = "default",
) -> PutStep:
    params = {"alert_type": alert_type, "message": title, "text": body}
    return PutStep(
        put=resource.name,
        params=params,
    )
