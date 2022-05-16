#!/bin/sh

set -e

AWS_ROLE=$(curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/)
export $(printf "AWS_ACCESS_KEY_ID=%s AWS_SECRET_ACCESS_KEY=%s AWS_SESSION_TOKEN=%s" \
$(aws sts assume-role \
--role-arn $AWS_ROLE \
--role-session-name MySessionName \
--query "Credentials.[AccessKeyId,SecretAccessKey,SessionToken]" \
--output text))
cd aws_creds
echo "[default]" > credentials
echo "aws_access_key_id=$AWS_ACCESS_KEY_ID" >> credentials
echo "aws_secret_access_key=$AWS_SECRET_ACCESS_KEY" >> credentials
echo "aws_session_token=$AWS_SESSION_TOKEN" >> credentials
