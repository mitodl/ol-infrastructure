Overview

This project will enable our Universal AI partners to consume the data we
generate via S3 directly or through an SFTP server we provide.

## Resources
- S3 bucket appropriately named for the project
- One prefix for each partner
- One SFTP username per partner. Partners should use SSH keys they
provide us to access their sftp account.

## Inputs
- A list of nartners including descriptive name, username and ssh public key for each

## Acceptance Criteria

- Each partner should only be able to access their own prefix in the S3 bucket via S3 and SFTP
- We should be able to add and remove partners easily

## Prior Art

You can utilize the SFTPServer component in infrastructure/aws/ and infrastructure/components/aws as examples.
