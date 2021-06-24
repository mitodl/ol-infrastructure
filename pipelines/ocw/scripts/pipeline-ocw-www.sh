#!/bin/bash

# This script requires prior authentication via target "odl-concourse"

# The following variables need to be set, and will be different between RC and production:
# GIT_CONFIG_ORG=<the github organization>
# GITHUB_DOMAIN=<the github domain>
# OCW_WWW_REPO=<the github repo for the ocw-www site>
# OCW_CONTENT_BUCKET_PREVIEW=<the S3 preview bucket>
# OCW_CONTENT_BUCKET_RELEASE=<the S3 release bucket>
# OCW_STUDIO_BASE_URL=<the ocw-studio base url>
# OCW_HUGO_PROJECTS_BRANCH=<'release-candidate' on RC, 'release' on production>

# The following need to be set via vault, per environment and pipeline group (preview, release):
# fastly-service-id
# fastly-api-token
# git-private-key


BRANCHES=("preview" "release")

for branch in "${BRANCHES[@]}"
do
  if [[ $branch == "preview" ]]
  then
    OCW_CONTENT_BUCKET=$OCW_CONTENT_BUCKET_PREVIEW
  elif [[ $branch == "release" ]]
  then
    OCW_CONTENT_BUCKET=$OCW_CONTENT_BUCKET_RELEASE
  else
    echo "Invalid branch $1"
    exit 1
  fi

  yes | fly -t odl-concourse set-pipeline \
  -p ocw-www-pipeline-via-vault \
  --team=ocw \
  --config=pipelines/ocw/pipeline-ocw-www.yml \
  --instance-var branch=$branch \
  -v git-domain=$GITHUB_DOMAIN \
  -v github-org=$GIT_CONFIG_ORG \
  -v ocw-www-repo=$OCW_WWW_REPO \
  -v ocw-www-repo-branch=$branch \
  -v ocw-bucket=$OCW_CONTENT_BUCKET \
  -v ocw-studio-url=$OCW_STUDIO_BASE_URL \
  -v ocw-hugo-projects-branch=$OCW_HUGO_PROJECTS_BRANCH
done