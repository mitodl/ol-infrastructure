"""Module for creating and managing S3 buckets that are not used by any applications."""

from pulumi_aws import s3

from ol_infrastructure.lib.ol_types import AWSBase

aws_config = AWSBase(tags={'OU': 'mitxpro', 'Environment': 'operations'})

s3.Bucket(
    'xpro-legacy-certificates-bucket',
    bucket='certificates.mitxpro.mit.edu',
    acl='public-read',
    tags=aws_config.tags,
    versioning={'enabled': True},
    cors_rules=[
        {
            'allowedMethods': ['GET', 'HEAD'],
            'allowedOrigins': ['*']
        }
    ],
    website={
        'indexDocument': 'index.html'
    },
)
