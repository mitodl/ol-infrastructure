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
