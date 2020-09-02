"""
Dynamic provider that uses the SaltStack API to provision a minion.

Registers a minion ID with a SaltStack master and generates a keypair.  The keypair is returned so that it can be passed
to an instance via user data to take advantage of cloud-init.
"""
from dataclasses import dataclass
from typing import Dict, Optional, Text

from pepper import Pepper
from pulumi import Input, Output, ResourceOptions
from pulumi.dynamic import CreateResult, ReadResult, Resource, ResourceProvider


@dataclass
class OLSaltStackInputs:
    minion_id: Input[Text]
    salt_api_url: Input[Text]
    salt_user: Input[Text]
    salt_password: Input[Text]
    salt_auth_method: Input[Text] = 'pam'


class OLSaltStackProvider(ResourceProvider):

    def create(self, inputs: Dict[Text, Text]) -> CreateResult:
        """Register a salt minion and generate a keypair to be returned via Outputs.

        :param inputs: A salt client and minion ID to interact with the Salt API
        :type inputs: _OLSaltStackProviderInputs

        :returns: The ID of the minion and its public/private keypair.

        :rtype: CreateResult
        """
        salt_client = self._salt_client(
            inputs['salt_api_url'],
            inputs['salt_user'],
            inputs['salt_password'],
            inputs['salt_auth_method']
        )
        keypair = salt_client.wheel(
            'key.gen_accept',
            id_=inputs['minion_id']
        )['return'][0]['data']['return']
        output = inputs.copy()
        output.update(
            {
                'minion_public_key': keypair['pub'],
                'minion_private_key': keypair['priv']
            }
        )
        return CreateResult(id_=inputs['minion_id'], outs=output)

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
            properties['salt_api_url'],
            properties['salt_user'],
            properties['salt_password'],
            properties['salt_auth_method']
        )
        keyinfo = salt_client.wheel(
            'key.print',
            match=[id_]
        )['return'][0]['data']['return']
        output = properties.copy()
        output.update(
            {
                'minion_public_key': keyinfo.get('minions', {}).get(id_)
            }
        )
        return ReadResult(id_=id_, outs=output)

    def delete(self, id_: Text, properties: Dict[Text, Text]):
        """Delete the salt minion key from the master.

        :param id_: The ID of the target minion
        :type id_: Text

        :param properties: The minion ID and salt API client
        :type properties: _OLSaltStackProviderInputs
        """
        salt_client = self._salt_client(
            properties['salt_api_url'],
            properties['salt_user'],
            properties['salt_password'],
            properties['salt_auth_method']
        )
        salt_client.wheel('key.delete', match=[id_])

    def _salt_client(
            self,
            api_url: Text,
            api_user: Text,
            api_password: Text,
            api_auth: Text = 'pam'
    ) -> Pepper:
        salt_client = Pepper(api_url)
        salt_client.login(
            username=api_user,
            password=api_password,
            eauth=api_auth
        )
        return salt_client


class OLSaltStack(Resource):
    minion_id: Output[Text]
    minion_public_key: Output[Optional[Text]]
    minion_private_key: Output[Optional[Text]]

    def __init__(self, name: Text, properties: OLSaltStackInputs, opts: ResourceOptions = None):
        resource_options = ResourceOptions.merge(
            ResourceOptions(additional_secret_outputs=['minion_private_key']),
            opts)  # type: ignore
        super().__init__(
            OLSaltStackProvider(),
            name,
            {
                'minion_id': properties.minion_id,
                'minion_public_key': None,
                'minion_private_key': None,
                'salt_api_url': properties.salt_api_url,
                'salt_user': properties.salt_user,
                'salt_password': properties.salt_password,
                'salt_auth_method': properties.salt_auth_method
            },
            resource_options
        )
