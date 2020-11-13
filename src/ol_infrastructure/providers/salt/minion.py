"""
Dynamic provider that uses the SaltStack API to provision a minion.

Registers a minion ID with a SaltStack master and generates a keypair.  The keypair is returned so that it can be passed
to an instance via user data to take advantage of cloud-init.

To use you can pass the API configuration as a Pulumi stack config or using environment variables.

API URL:
  As pulumi config: saltstack:api_url
  As environment variable: SALTSTACK_API_URL

API User:
  As pulumi config: saltstack:api_user
  As environment variable: SALTSTACK_API_USER

API Password:
  As pulumi config: saltstack:api_password
  As environment variable: SALTSTACK_API_PASSWORD

API Authentication Method:
  As pulumi config: saltstack:api_auth_method
  As environment variable: SALTSTACK_API_AUTH_METHOD
"""
import os
from dataclasses import dataclass
from typing import Dict, Optional, Text

from pepper import Pepper
from pulumi import Config, Input, Output, ResourceOptions
from pulumi.dynamic import CreateResult, ReadResult, Resource, ResourceProvider


@dataclass
class OLSaltStackMinionInputs:
    minion_id: Input[Text]
    salt_api_url: Optional[Input[Text]] = None
    salt_user: Optional[Input[Text]] = None
    salt_password: Optional[Input[Text]] = None
    salt_auth_method: Input[Text] = "pam"

    def __post_init__(self) -> None:
        salt_config = Config("saltstack")
        env_map = {
            "salt_api_url": ["api_url", "SALTSTACK_API_URL"],
            "salt_user": ["api_user", "SALTSTACK_API_USER"],
            "salt_password": ["api_password", "SALTSTACK_API_PASSWORD"],
            "salt_auth_method": ["api_auth_method", "SALTSTACK_API_AUTH_METHOD"],
        }
        for attr, lookups in env_map.items():
            if getattr(self, attr) is None:
                setattr(
                    self,
                    attr,
                    salt_config.get(lookups[0]) or os.environ.get(lookups[1]),
                )  # noqa: WPS221
                if not getattr(self, attr):
                    raise ValueError(
                        "The SaltStack minion provider is missing a required parameter. "
                        f"Please set the Pulumi config saltstack:{lookups[0]} or set the "
                        f"environment variable {lookups[1]}",  # noqa: WPS326
                    )


class OLSaltStackMinionProvider(ResourceProvider):
    def create(self, inputs: Dict[Text, Text]) -> CreateResult:
        """Register a salt minion and generate a keypair to be returned via Outputs.

        :param inputs: A salt client and minion ID to interact with the Salt API
        :type inputs: _OLSaltStackProviderInputs

        :returns: The ID of the minion and its public/private keypair.

        :rtype: CreateResult
        """
        salt_client = self._salt_client(
            inputs["salt_api_url"],
            inputs["salt_user"],
            inputs["salt_password"],
            inputs["salt_auth_method"],
        )
        keypair = salt_client.wheel("key.gen_accept", id_=inputs["minion_id"])[
            "return"
        ][0]["data"]["return"]
        output = inputs.copy()
        output.update(
            {"minion_public_key": keypair["pub"], "minion_private_key": keypair["priv"]}
        )
        return CreateResult(id_=inputs["minion_id"], outs=output)

    def read(self, id_: Text, properties: Dict[Text, Text]) -> ReadResult:
        """Retrieve the ID and public key of the target minion from the Salt API.

        :param id_: The minion ID
        :type id_: Text

        :param properties: The salt client and minion ID
        :type properties: _OLSaltStackProviderInputs

        :returns: The minion ID and public key

        :rtype: ReadResult
        """
        salt_client = self._salt_client(
            properties["salt_api_url"],
            properties["salt_user"],
            properties["salt_password"],
            properties["salt_auth_method"],
        )
        keyinfo = salt_client.wheel("key.print", match=[id_])["return"][0]["data"][
            "return"
        ]
        output = properties.copy()
        output.update({"minion_public_key": keyinfo.get("minions", {}).get(id_)})
        return ReadResult(id_=id_, outs=output)

    def delete(self, id_: Text, properties: Dict[Text, Text]):
        """Delete the salt minion key from the master.

        :param id_: The ID of the target minion
        :type id_: Text

        :param properties: The minion ID and salt API client
        :type properties: _OLSaltStackProviderInputs
        """
        salt_client = self._salt_client(
            properties["salt_api_url"],
            properties["salt_user"],
            properties["salt_password"],
            properties["salt_auth_method"],
        )
        salt_client.wheel("key.delete", match=[id_])

    def _salt_client(
        self, api_url: Text, api_user: Text, api_password: Text, api_auth: Text = "pam"
    ) -> Pepper:
        salt_client = Pepper(api_url)
        salt_client.login(username=api_user, password=api_password, eauth=api_auth)
        return salt_client


class OLSaltStackMinion(Resource):
    minion_id: Output[Text]
    minion_public_key: Output[Optional[Text]]
    minion_private_key: Output[Optional[Text]]

    def __init__(
        self,
        name: Text,
        properties: OLSaltStackMinionInputs,
        opts: ResourceOptions = None,
    ):
        resource_options = ResourceOptions.merge(  # type: ignore
            ResourceOptions(additional_secret_outputs=["minion_private_key"]), opts
        )
        super().__init__(
            OLSaltStackMinionProvider(),
            name,
            {
                "minion_id": properties.minion_id,
                "minion_public_key": None,
                "minion_private_key": None,
                "salt_api_url": properties.salt_api_url,
                "salt_user": properties.salt_user,
                "salt_password": properties.salt_password,
                "salt_auth_method": properties.salt_auth_method,
            },
            resource_options,
        )
