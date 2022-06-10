#!/bin/bash

TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
PRIVATE_IPV4=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/local-ipv4)
VAULT_CLUSTER_ADDR="https://$PRIVATE_IPV4:8201"
VAULT_KMS_KEY_ID=$(cat /var/opt/kms_key_id)
echo "VAULT_CLUSTER_ADDR=$VAULT_CLUSTER_ADDR" | tee /etc/default/vault
echo "VAULT_AWSKMS_SEAL_KEY_ID=$VAULT_KMS_KEY_ID" | tee -a /etc/default/vault
systemctl restart vault
