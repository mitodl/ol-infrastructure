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
            "memory": "128Mi",
        },
        "limits": {
            "cpu": "100m",
            "memory": "512Mi",
        },
    },
}


def get_prepuller_config_with_images(images):
    config = PREPULLER_CONFIG.copy()
    config["extraImages"] = images
    return config
