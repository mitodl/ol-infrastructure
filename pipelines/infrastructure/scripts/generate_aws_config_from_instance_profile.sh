#!/bin/sh

AWS_ROLE=$(aws sts get-caller-identity | grep 'arn' |  perl -ne 'print $1 if  /(arn.*)\//')
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
