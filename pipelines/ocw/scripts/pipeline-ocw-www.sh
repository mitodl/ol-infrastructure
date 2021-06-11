#!/bin/bash

SITE_BRANCH=$1  # Should be "preview" or "release"
if [[ $SITE_BRANCH == "preview" ]]
then
  FASTLY_SERVICE_ID=$FASTLY_SERVICE_ID_PREVIEW
  FASTLY_API_TOKEN=$FASTLY_API_TOKEN_PREVIEW
  OCW_CONTENT_BUCKET=$OCW_CONTENT_BUCKET_PREVIEW
elif [[ $SITE_BRANCH == "release" ]]
then
  FASTLY_SERVICE_ID=$FASTLY_SERVICE_ID_RELEASE
  FASTLY_API_TOKEN=$FASTLY_API_TOKEN_RELEASE
  OCW_CONTENT_BUCKET=$OCW_CONTENT_BUCKET_RELEASE
else
  echo "Invalid branch $1"
  exit 1
fi

yes | fly -t $FLY_TARGET set-pipeline \
-p ocw-www-pipeline-$SITE_BRANCH \
--team=$FLY_TEAM \
--config=pipelines/ocw/pipeline-ocw-www.yml \
-v artifact-bucket=$ARTIFACT_BUCKET \
-v git-domain=$GITHUB_DOMAIN \
-v github-org=$GIT_CONFIG_ORG \
-v hugo-theme-branch=$HUGO_THEME_BRANCH \
-v ocw-www-repo=$OCW_WWW_REPO \
-v ocw-www-repo-branch=$SITE_BRANCH \
-v ocw-bucket=$OCW_CONTENT_BUCKET \
-v ocw-studio-url=$OCW_STUDIO_BASE_URL \
-v fastly-service-id=$FASTLY_SERVICE_ID \
-v fastly-api-token=$FASTLY_API_TOKEN \
-v git-private-key="$GIT_PRIVATE_KEY"
