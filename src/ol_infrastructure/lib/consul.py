import base64
import json

import yaml


def build_consul_userdata(env_name):
    """Build consul userdata for cloud init, to create auto scale group and launch configs.

    :param env_name: consul environment being built
    :returns: base64-encoded yaml cloud init data

    TODO setup for consul_esm
    """
    return base64.b64encode(
        "#cloud-config\n{}".format(
            yaml.dump(
                {
                    "write_files": [
                        {
                            "path": "/etc/consul.d/99-autojoin.json",
                            "content": json.dumps(
                                {
                                    "retry_join": [
                                        "provider=aws tag_key=consul_env "
                                        f"tag_value={env_name}"
                                    ],
                                    "datacenter": env_name,
                                }
                            ),
                            "owner": "consul:consul",
                        },
                        {
                            "path": "/etc/consul.d/99-autojoin-wan.json.json",
                            "content": json.dumps(
                                {
                                    "retry_join_wan": [
                                        "provider=aws tag_key=consul_env "
                                        f"tag_value={env_name}"
                                    ],
                                    "datacenter": env_name,
                                }
                            ),
                            "owner": "consul:consul",
                        },
                        """
                        TODO add consul esm config
                         {
                            "path": "/etc/consul-esm.d/config.json",
                            "content": json.dumps(
                                {
                                    "retry_join": [
                                        "provider=aws tag_key=consul_env "
                                        f"tag_value={env_name}"
                                    ],
                                    "datacenter": env_name,
                                }
                            ),
                            "owner": "consul:consul",
                        },
                        """,
                    ]
                },
                sort_keys=True,
            )
        ).encode("utf8")
    ).decode("utf8")
