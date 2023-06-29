# Moira Certificates

There are two services in our porfolio that interact with [Moira](https://ist.mit.edu/email-lists).

1. MIT Open / OpenDiscussions
2. ODL-Video-Service

Both of these services authenticate against Moira with certificates issued by [ca.mit.edu](https://ca.mit.edu/ca/) which is also the provider of MIT Personal Certificates. There is no web interface for requesting an application certificate from ca.mit.edu, so you need to email mitcert@mit.edu with the CSR in the body of the email and a clear request that you're asking for a certificate issued from ca.mit.edu and NOT InCommon/Internet2 which is where most MIT certificates now come from.

Both of these applications utilize the same two environment variables for storing and accessing this key/cert pair.

- **MIT_WS_CERTIFICATE**
- **MIT_WS_PRIVATE_KEY**

## MITOpen Vault Locations

- In all vault environments: `secret-mit-open/global/mit-application-certificate`
- Maintained by hand.

## ODL Video Service Vault Location

- In all vault environments: `secret-odl-video-service/ovs/secrets`
  - Inside a single JSON structure at `misc.mit_ws_certificate` and `misc.mit_ws_private_key`
- Maintained automatically by pulumi.
  - `sr/bridge/secrets/odl_video_service` or [here](https://github.com/mitodl/ol-infrastructure/tree/main/src/bridge/secrets/odl_video_service)
