#!/bin/bash

# A dirty script to reindex data from the old, manually created elasticsearch
# cluster for open to a new, managed service cluster

# addresses MacOS curl + openssl oddity
export CURL_SSL_BACKEND="secure-transport"

# Fill these in
export SOURCE="https://search-opensearch-mitopen-productio-34bmfnsi2zdrngie5vdpe7keu4.us-east-1.es.amazonaws.com:443"
export SOURCE_PW=""
export SOURCE_USER="opensearch"
export DEST="https://search-opensearch-mitlearn-producti-qjtnw2dcoampmbtscs3wdfci6y.us-east-1.es.amazonaws.com:443"
export DEST_PW=""
export DEST_USER="opensearch"
# Can be easily populated with this:
# curl $SOURCE/_cat/indices | awk '{print"\"" $3 "\""}'
index_names=(
)

if [ -z "$SOURCE" ]; then
  echo "ERROR: source address not provided."
  exit 1
fi
if [ -z "$SOURCE_PW" ]; then
  echo "ERROR: source password not provided."
  exit 1
fi
if [ -z "$SOURCE_USER" ]; then
  echo "ERROR: source username not provided."
  exit 1
fi
if [ -z "$DEST" ]; then
  echo "ERROR: destination not provided."
  exit 1
fi
if [ -z "$DEST_PW" ]; then
  echo "ERROR: destination password not provided."
  exit 1
fi
if [ -z "$DEST_USER" ]; then
  echo "ERROR: destination username not provided."
  exit 1
fi

for index in "${index_names[@]}"; do
  echo "Processing $index"##
  index_def=$(curl -s -X GET -u "$SOURCE_USER:$SOURCE_PW" "$SOURCE/$index/" |  jq -r --arg index "$index" '."\($index)" | del(.settings.index.creation_date, .settings.index.provided_name, .settings.index.uuid, .settings.index.version)')
  echo "creating ${index_def}"
  curl -X PUT -H 'Content-Type: application/json' -u "$DEST_USER:$DEST_PW" "$DEST/$index" -d "$index_def"
  echo ""
  echo "Starting reindex of $index from $SOURCE to $DEST."
  echo ""
  read -r -d '' data_body <<EOF
{
  "source": {
    "size": 100,
    "remote": {
      "host": "${SOURCE}",
      "username": "${SOURCE_USER}",
      "password": "${SOURCE_PW}",
      "socket_timeout": "5m",
      "connect_timeout": "5m"
    },
    "index": "${index}"
  },
  "dest": {
    "index": "${index}",
    "op_type": "index"
  }
}
EOF
  curl -X POST -H 'Content-Type: application/json' -u "$DEST_USER:$DEST_PW" "$DEST/_reindex?wait_for_completion=false" -d "$data_body"
done
