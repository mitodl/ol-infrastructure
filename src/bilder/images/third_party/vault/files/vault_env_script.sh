#!/bin/bash

PRIVATE_IPV4=$(curl http://169.254.169.254/latest/meta-data/local-ipv4)
VAULT_CLUSTER_ADDR="https://$PRIVATE_IPV4:8201"
VAULT_KMS_KEY_ID=$(cat /var/opt/kms_key_id)
echo "VAULT_CLUSTER_ADDR=$VAULT_CLUSTER_ADDR" | tee /etc/default/vault
echo "VAULT_AWSKMS_SEAL_KEY_ID=$VAULT_KMS_KEY_ID" | tee -a /etc/default/vault
systemctl restart vault
