"""JupyterHub Helm values and configuration helpers for the jupyterhub stack.

Authenticator helpers (``get_authenticator_config``, ``InvalidAuthenticatorError``)
have been moved to :mod:`ol_infrastructure.lib.jupyterhub_config` and are
re-exported here for backward compatibility.
"""

from ol_infrastructure.lib.jupyterhub_config import (
    InvalidAuthenticatorError,
    get_authenticator_config,
)

__all__ = ["InvalidAuthenticatorError", "get_authenticator_config"]

PREPULLER_CONFIG = {
    "continuous": {
        "enabled": True,
    },
    "hook": {
        "enabled": False,
    },
    "extraImages": {},
    "resources": {
        "requests": {
            "cpu": "10m",
            "memory": "64Mi",
        },
        "limits": {
            "memory": "64Mi",
        },
    },
}

DISABLE_PREPULLER_CONFIG = {
    "continuous": {
        "enabled": False,
    },
    "hook": {
        "enabled": False,
    },
}


def get_prepuller_config_for_images(images):
    if images:
        config = PREPULLER_CONFIG.copy()
        config["extraImages"] = images
        return config
    else:
        return DISABLE_PREPULLER_CONFIG.copy()
