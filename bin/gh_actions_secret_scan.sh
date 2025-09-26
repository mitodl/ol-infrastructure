#!/bin/bash

ORG="mitodl"
REPO_LIST=$(gh repo list "$ORG" --json name --limit 1000)

for repo in $(echo "$REPO_LIST" | jq -r '.[].name'); do
  echo "Checking repository: $repo"
  SECRET_NAMES=$(gh secret list --repo "$ORG/$repo" --json name)

  echo "$SECRET_NAMES" | jq -r '.[].name' | while read -r secret; do
    if [[ "$secret" =~ (NPM|TOKEN|AUTH|GH|GITHUB|AWS) ]]; then
      echo "  Potential token found: $secret"
    fi
  done
done
