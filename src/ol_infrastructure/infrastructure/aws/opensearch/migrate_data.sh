#!/bin/bash

# A dirty script to reindex data from the old, manually created elasticsearch
# cluster for open to a new, managed service cluster

# Prereqs
#
# On EVERY NODE in the source cluster make the following updates
#
# 0. Backup the existing config
#    `cp /etc/elasticsearch/readonlyrest.yml ~`
#    `cp /etc/nginx/sites-available/elasticsearch ~`
#
# 1. /etc/elasticsearch/readonlyrest.yml add the following permission block
#
#  - actions:
#    - '*'
#    auth_key: $SOURCE_USER:$SOURCE_PW
#    indices:
#    - '*'
#    type: allow
#    name: reindex_user
#
#   Where $SOURCE_USER and $SOURCE_PW are the username and password you intend
#   create on the source environment for copying data.
#
# 2. restart elasticsearch, one node at a time `systemctl restart elasticsearch`
#    make sure the cluster is green between each restart
#
# 3. /etc/nginx/sites-available/elasticsearch add the following block after the first
#    location directive (the one with all the paths specified)
#
#   location ~ ^/$ {
#        proxy_pass http://127.0.0.1:9200$request_uri;
#        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
#        proxy_pass_header X-Api-Key;
#        client_max_body_size 75m;
#   }
#
#   The reindex process running on the destination environment wants to be able to
#   verify that the source is actually an ES cluster and what it supports, which
#   requires the / endpoint respond. That endpoint doesn't respond with our current
#   config.
#
# 4. Restart nginx `systemctl restart nginx`
#
# 5. WHen you're done restore the original configs on each node
#    `cp ~/readonlyrest.yml /etc/elasticsearch/readonlyrest.yml;`
#    `cp ~/elasticsearch /etc/nginx/sites-available/elasticsearch`
#    `systemctl restart elasticsearch`  # ONE NODE AT A TIME, Verify green status between each!
#    `systemctl restart nginx`

# addresses MacOS curl + openssl oddity
export CURL_SSL_BACKEND="secure-transport"

# Fill these in
export SOURCE=""
export SOURCE_PW=""  # Possibly not actually required
export SOURCE_USER="" # Possibly not actually required
export DEST=""
export DEST_PW=""
export DEST_USER=""
# Can be easily populated with this:
# curl $SOURCE/_cat/indices | awk '{print"\"" $3 "\""}'
index_names=()


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
  echo "Processing $index"
  index_def=$(curl -s -X GET "$SOURCE/$index/" |  jq -r --arg index "$index" '."\($index)" | del(.settings.index.creation_date, .settings.index.provided_name, .settings.index.uuid, .settings.index.version)')
  echo "Creating $index at $DEST"
  curl -X PUT -H 'Content-Type: application/json' -u "$DEST_USER:$DEST_PW" "$DEST/$index" -d "$index_def"
  echo ""
  echo "Starting reindex of $index from $SOURCE to $DEST."
  echo ""
  read -r -d '' data_body <<EOF
{
  "source": {
    "size": 500,
    "remote": {
      "host": "${SOURCE}",
      "username": "${SOURCE_USER}",
      "password": "${SOURCE_PW}",
      "socket_timeout": "5m",
      "connect_timeout": "60s",
      "external": true
    },
    "index": "${index}"
  },
  "dest": {
    "index": "${index}"
  }
}
EOF
  curl -X POST -H 'Content-Type: application/json' -u "$DEST_USER:$DEST_PW" "$DEST/_reindex?wait_for_completion=false" -d "$data_body"
done
