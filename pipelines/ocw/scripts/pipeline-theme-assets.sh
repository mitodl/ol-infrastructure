#!/bin/bash

# This script requires prior authentication via target "odl-concourse"

BRANCHES=("release" "release-candidate")

for branch in "${BRANCHES[@]}"
do
  yes | fly -t odl-concourse set-pipeline \
  -p ocw-theme-assets \
  --team=ocw \
  --config=pipelines/ocw/pipeline-theme-assets.yml \
  --instance-var branch=$branch \
  --load-vars-from pipelines/ocw/vars/pipeline-theme-assets/$branch.yml
done
