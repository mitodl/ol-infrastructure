"""
Dynamic provider that uses the SaltStack API to provision a minion.

Registers a minion ID with a SaltStack master and generates a keypair.  The keypair is returned so that it can be passed
to an instance via user data to take advantage of cloud-init.
"""
from typing import Optional, Text

from pepper import Pepper
from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import CreateResult, ReadResult, Resource, ResourceProvider
from pydantic import BaseModel, SecretStr


class OLSaltStackInputs(BaseModel):
    minion_id: Input[Text]
    salt_api_url: Input[Text]
    salt_user: Input[Text]
    salt_password: Input[SecretStr]
    salt_auth_method: Input[Text] = 'pam'


class _OLSaltStackProviderInputs(BaseModel):
    minion_id: Text
    salt_client: Pepper


class OLSaltStackProvider(ResourceProvider):

    def create(self, inputs: _OLSaltStackProviderInputs) -> CreateResult:
        """Register a salt minion and generate a keypair to be returned via Outputs.

        :param inputs: A salt client and minion ID to interact with the Salt API
        :type inputs: _OLSaltStackProviderInputs

        :returns: The ID of the minion and its public/private keypair.

        :rtype: CreateResult
        """
        keypair = inputs.salt_client.wheel('key.gen_accept', inputs.minion_id)
        output = {
            'minion_id': inputs.minion_id,
            'minion_public_key': keypair['pub'],
            'minion_private_key': keypair['priv']
        }
        return CreateResult(id_=inputs.minion_id, outs=output)

    def read(self, id_: Text, properties: _OLSaltStackProviderInputs) -> ReadResult:
        """Retrieve the ID and public key of the target minion from the Salt API.

        :param id_: The minion ID
        :type id_: Text

        :param properties: The salt client and minion ID
        :type properties: _OLSaltStackProviderInputs

        :returns: The minion ID and public key

        :rtype: ReadResult
        """
        keyinfo = properties.salt_client.wheel('key.key_str', [id_])
        output = {
            'minion_id': id_,
            'minion_public_key': keyinfo['minions'][id_]
        }
        return ReadResult(id_=id_, outs=output)

    def delete(self, id_: Text, properties: _OLSaltStackProviderInputs):
        """Delete the salt minion key from the master.

        :param id_: The ID of the target minion
        :type id_: Text

        :param properties: The minion ID and salt API client
        :type properties: _OLSaltStackProviderInputs
        """
        properties.salt_client.wheel('key.delete', id_)


class OLSaltStack(Resource):
    minion_id: Output[Text]
    minion_public_key: Output[Text]
    minion_private_key: Optional[Output[Text]]

    def __init__(self, name: Text, properties: OLSaltStackInputs, opts: ResourceOptions = None):
        resource_options = ResourceOptions.merge(
            ResourceOptions(additional_secret_outputs=['minion_private_key']),
            opts)  # type: ignore
        salt_client = Pepper(properties.salt_api_url)
        salt_client.login(
            username=properties.salt_user,
            password=properties.salt_password,
            eauth=properties.salt_auth_method
        )
        super().__init__(
            OLSaltStackProvider(),
            name,
            {'minion_id': properties.minion_id,
             'public_key': None,
             'private_key': None,
             'salt_client': salt_client
             },
            resource_options
        )
