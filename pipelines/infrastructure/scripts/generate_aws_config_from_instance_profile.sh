#!/bin/sh

set -e

AWS_ROLE=$(curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/)
export AWS_ACCESS_KEY_ID=$(curl http://169.254.169.254/latest/meta-data/iam/security-credentials/$AWS_ROLE | awk '/AccessKeyId/ {print $3}' | sed 's/[",]//g')
export AWS_SECRET_ACCESS_KEY=$(curl http://169.254.169.254/latest/meta-data/iam/security-credentials/$AWS_ROLE | awk '/SecretAccessKey/ {print $3}' | sed 's/[",]//g')
export AWS_SESSION_TOKEN=$(curl http://169.254.169.254/latest/meta-data/iam/security-credentials/$AWS_ROLE | awk '/Token/ {print $3}' | sed 's/[",]//g')
cd aws_creds
echo "[default]" > credentials
echo "aws_access_key_id=$AWS_ACCESS_KEY_ID" >> credentials
echo "aws_secret_access_key=$AWS_SECRET_ACCESS_KEY" >> credentials
echo "aws_session_token=$AWS_SESSION_TOKEN" >> credentials
