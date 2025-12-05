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


class InvalidAuthenticatorError(Exception):
    def __init__(self, authenticator_type):
        super().__init__(f"Invalid authenticator type: {authenticator_type}")


# Authenticators all have slightly different semantics for which traitlets
# are used from where. This function needs to spit out the entire block
# At the moment this spits out the hub.config block,
# which we only use for authenticator configuration.
def get_authenticator_config(jupyterhub_deployment_config):
    authenticator_type = jupyterhub_deployment_config.get("authenticator_class", "tmp")
    if authenticator_type not in ["shared-password", "tmp"]:
        raise InvalidAuthenticatorError(authenticator_type)

    admin_users_list = jupyterhub_deployment_config.get("admin_users", [])
    allowed_users_list = jupyterhub_deployment_config.get("allowed_users", [])
    auth_conf = {
        "Authenticator": {
            "admin_users": admin_users_list,
            "allowed_users": allowed_users_list,
        }
    }
    if authenticator_type == "shared-password":
        auth_conf["SharedPasswordAuthenticator"] = {
            "user_password": jupyterhub_deployment_config.get("shared_password")
        }
        auth_conf["JupyterHub"] = {
            "authenticator_class": "shared-password",
        }
    elif authenticator_type == "tmp":
        auth_conf["JupyterHub"] = {"authenticator_class": "tmp"}
    return auth_conf
