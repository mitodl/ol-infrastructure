"""HTTP helpers shared by the dynamic providers."""

import requests


def raise_for_status_with_body(response: requests.Response) -> None:
    """Raise on a non-2xx response, keeping the body in the message.

    APIs explain a 400 or 403 in the body (a validation or authorization
    message), which ``raise_for_status()`` alone would drop.

    :param response: The response to check.
    :raises RuntimeError: If the response is not 2xx.
    """
    if not response.ok:
        msg = (
            f"{response.request.method} {response.url} returned "
            f"{response.status_code}: {response.text}"
        )
        raise RuntimeError(msg)
