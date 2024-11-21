# Reference: https://hvac.readthedocs.io/en/stable/usage/auth_methods/aws.html#ec2-authentication

import logging.handlers
import os

import hvac
import requests

logger = logging.getLogger(__name__)

VAULT_URL = os.getenv("VAULT_ADDR", "https://127.0.0.1:8200")
TOKEN_NONCE_PATH = os.getenv(
    "VAULT_TOKEN_NONCE_PATH", "/app/superset_home/.vault-token-meta-nonce"
)
EC2_METADATA_URL_BASE = "http://169.254.169.254"


def load_aws_ec2_pkcs7_string(metadata_url_base=EC2_METADATA_URL_BASE):
    """
    Request an ec2 instance's pkcs7-encoded identity document from EC2 metadata service.

    :param metadata_url_base: IP address for the EC2 metadata service.

    :return: string, pkcs7-encoded identity document from the EC2 metadata service
    """

    metadata_pkcs7_url = f"{metadata_url_base}/latest/dynamic/instance-identity/pkcs7"
    logger.debug("load_aws_ec2_pkcs7_string connecting to %s", metadata_pkcs7_url)

    response = requests.get(url=metadata_pkcs7_url)  # noqa: S113
    response.raise_for_status()

    pcks7 = response.text.replace("\n", "")

    return pcks7  # noqa: RET504


def load_aws_ec2_nonce_from_disk(token_nonce_path=TOKEN_NONCE_PATH):
    """
    Helper method to load a previously stored "token_meta_nonce" returned in the initial
    authorization AWS EC2 request from the current instance to our Vault service.

    :param token_nonce_path: string, the full filesystem path to a file containing the
        instance's token meta nonce.

    :return: string, a previously stored "token_meta_nonce"
    """  # noqa: D401
    logger.debug(
        "Attempting to load vault token meta nonce from path: %s", token_nonce_path
    )
    try:
        with open(token_nonce_path) as nonce_file:  # noqa: PTH123
            nonce = nonce_file.readline()
    except OSError:
        logger.warning(
            "Unable to load vault token meta nonce at path: %s", token_nonce_path
        )
        nonce = None

    logger.debug("Nonce loaded: %s", nonce)
    return nonce


def write_aws_ec2_nonce_to_disk(token_meta_nonce, token_nonce_path=TOKEN_NONCE_PATH):
    """
    Helper method to store the current "token_meta_nonce" returned from authorization
    AWS EC2 request from the current instance to our Vault service.

    :return: string, a previously stored "token_meta_nonce"

    :param token_meta_nonce: string, the actual nonce
    :param token_nonce_path: string, the full filesystem path to a file containing the
        instance's token meta nonce.

    :return: None
    """  # noqa: D401
    logger.debug('Writing nonce "%s" to file "%s".', token_meta_nonce, token_nonce_path)
    with open(token_nonce_path, "w") as nonce_file:  # noqa: PTH123
        nonce_file.write(token_meta_nonce)


def auth_ec2(  # noqa: PLR0913
    vault_client,
    pkcs7=None,
    nonce=None,
    role=None,
    mount_point="aws",
    store_nonce=True,  # noqa: FBT002
):
    """
    Helper method to authenticate to vault using the "auth_ec2" backend.

    :param vault_client: hvac.Client
    :param pkcs7: pkcs7-encoded identity document from the EC2 metadata service
    :param nonce: string, the nonce retruned from the initial AWS EC2 auth request (if
        applicable)
    :param role: string, the role/policy to request.  Defaults to the current instance's
        AMI ID if not provided.
    :param mount_point: string, the path underwhich the AWS EC2 auth backend is provided
    :param store_nonce: bool, if True, store the nonce received in the auth_ec2 response
        on disk for later use.  Especially useful for automated secure introduction.
    :param kwargs: dict, remaining arguments blindly passed through by this lookup
        module class

    :return: None
    """  # noqa: D401
    if pkcs7 is None:
        logger.debug("No pkcs7 argument provided to auth_ec2 backend.")
        logger.debug("Attempting to retrieve information from EC2 metadata service.")
        pkcs7 = load_aws_ec2_pkcs7_string()

    if nonce is None:
        logger.debug("No nonce argument provided to auth_ec2 backend.")
        logger.debug("Attempting to retrieve information from disk.")
        nonce = load_aws_ec2_nonce_from_disk()

    auth_ec2_resp = vault_client.auth.aws.ec2_login(
        pkcs7=pkcs7, nonce=nonce, role=role, use_token=False, mount_point=mount_point
    )

    if store_nonce and "metadata" in auth_ec2_resp.get("auth", dict()):  # noqa: C408
        token_meta_nonce = auth_ec2_resp["auth"]["metadata"].get("nonce")
        if token_meta_nonce is not None:
            logger.debug(
                "token_meta_nonce received back from auth_ec2 call: %s",
                token_meta_nonce,
            )
            write_aws_ec2_nonce_to_disk(token_meta_nonce)
        else:
            logger.warning("No token meta nonce returned in auth response.")

    return auth_ec2_resp


def load_vault_token(vault_client, ec2_role=None):
    """
    Retrieves a vault token, first from a "VAULT_TOKEN" env var if available.  If this
    env var is unavailable, we use a Vault authentication backend to retrieve a token
    (currently limited to AWS EC2 authentication)

    :param vault_client: hvac.Client
    :param ec2_role: str, Name of the Vault AWS auth backend role to use when retrieving
        a token (if applicable)

    :return: string, a vault token
    """  # noqa: D401
    vault_token = os.environ.get("VAULT_TOKEN")
    if vault_token is None and ec2_role is not None:
        auth_ec2_resp = auth_ec2(
            vault_client=vault_client,
            role=ec2_role,
        )
        logger.debug(f"auth_ec2_resp:\n{auth_ec2_resp}")  # noqa: G004
        vault_token = auth_ec2_resp["auth"]["client_token"]
    return vault_token


def get_vault_client(
    vault_url=VAULT_URL,
    ec2_role=None,
):
    """
    Instantiates a hvac / vault client.

    :param vault_url: string, protocol + address + port for the vault service
    :param certs: tuple, Optional tuple of self-signed certs to use for verification
        with hvac's requests
    :param verify_certs: bool, if True use the provided certs tuple for verification
        with hvac's requests.  If False, don't verify SSL with hvac's requests
        (typically used with local development).
    :param ec2_role: str, Name of the Vault AWS auth backend role to use when retrieving
        a token (if applicable)

    :return: hvac.Client
    """  # noqa: D401
    logger.debug("Retrieving a vault (hvac) client...")
    vault_client = hvac.Client(
        url=vault_url,
    )
    vault_client.token = load_vault_token(vault_client, ec2_role=ec2_role)

    if not vault_client.is_authenticated():
        raise hvac.exceptions.Unauthorized(  # noqa: TRY003
            "Unable to authenticate to the Vault service"  # noqa: EM101
        )

    return vault_client
