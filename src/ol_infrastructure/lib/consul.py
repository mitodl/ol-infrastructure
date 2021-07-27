import base64
import json
import textwrap
import yaml


def build_consul_userdata(env_name):
    """ Builds consul userdata for cloud init, to create auto scale group and launch configs
        TODO setup for consul_esm """
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
                        """
                    ]
                },
                sort_keys=True,
            )
        ).encode("utf8")
    ).decode("utf8")
