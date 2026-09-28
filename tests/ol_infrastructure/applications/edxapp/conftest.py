"""Collection-time AWS defaults for the edxapp test package.

The session fixture in tests/conftest.py sets these too, but it runs at test
setup -- too late for a test module that imports an application module at
collection time. ol_infrastructure.lib.aws.elasticache_helper builds a boto3
client on import, which raises NoRegionError without a region, so importing
anything under applications.edxapp that reaches it fails during collection.
Defaults only: a real value in the environment still wins.
"""

import os

for _name, _value in {
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # pragma: allowlist secret
    "AWS_SECURITY_TOKEN": "testing",  # pragma: allowlist secret
    "AWS_SESSION_TOKEN": "testing",  # pragma: allowlist secret
}.items():
    os.environ.setdefault(_name, _value)
