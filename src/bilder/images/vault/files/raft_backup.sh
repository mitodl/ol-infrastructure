#!/bin/bash

export VAULT_SKIP_VERIFY="true"

raft_backup_dir='/tmp/backup/raft'
current_time=$(date "+%Y.%m.%d-%H.%M.%S")

VAULT_BIN=$(which vault)
AWS_BIN=$(which aws)
CURL_BIN=$(which curl)

log_message() {
  logger -t 'raft_backup' "$1"
}

clean_up() {
  log_message "INFO: cleaning up."
  rm -rf "$raft_backup_dir/$current_time.snapshot"
}

if [ "$($VAULT_BIN status --format json | jq --raw-output 'select(.is_self==true) | .is_self')" = "true" ]; then
  if [ ! -d $raft_backup_dir ]; then
    mkdir -p $raft_backup_dir
  fi

  if [ -z "$BUCKET_NAME" ]; then
    log_message "ERROR: Bucket name not provided."
    exit 1
  fi
  if [ -z "$HEALTH_CHECK_ID" ]; then
    log_message "ERROR: Healthchecks.io ID not provided."
    exit 1
  fi

  log_message "INFO: Requesting vault token."
  VAULT_TOKEN=$($VAULT_BIN login --method aws --path aws -no-store -token-only role=raft-backup)
  if [ -z "$VAULT_TOKEN" ]; then
    log_message "ERROR: Failed to obtain vault token."
    exit 1
  fi
  export VAULT_TOKEN
  log_message "INFO: Vault token received."

  log_message "INFO: Making raft snapshot $raft_backup_dir/$current_time"
  if ! $VAULT_BIN operator raft snapshot save "$raft_backup_dir/$current_time.snapshot"; then
    log_message "ERROR: Could not take raft snapshot."
    clean_up
    exit 1
  fi
  log_message "INFO: raft snapshot $raft_backup_dir/$current_time.snapshot complete"

  if ! $AWS_BIN s3 cp --quiet --no-progress  "$raft_backup_dir/$current_time.snapshot" "s3://$BUCKET_NAME/"; then
    log_message "ERROR: Could not upload snapshot to s3."
    clean_up
    exit 1
  fi
  log_message "INFO: snapshot $current_time.snapshot uploaded to s3 successfully."

  log_message "INFO: updating healthchecks.io"
  if ! $CURL_BIN -fsS -m 10 --retry 5 -o /dev/null "https://hc-ping.com/$HEALTH_CHECK_ID"; then
     log_message "ERROR: Could not update healthchecks.io"
     clean_up
     exit 1
  fi

  log_message "INFO: Revoking vault token."
  if ! $VAULT_BIN token revoke -self; then
    log_message "WARNING: Could not revoke own token."
  fi

  clean_up
  log_message "INFO: Raft snapshot successful and uploaded to s3. Exiting."
  exit 0
else
  log_message "INFO: Node not leader, not making raft snapshot."
  exit 0
fi
