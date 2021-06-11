#!/bin/bash

yes | fly -t $FLY_TARGET set-pipeline \
-p ocw-theme-assets-$HUGO_THEME_BRANCH \
--team=$FLY_TEAM \
--config=pipelines/ocw/pipeline-theme-assets.yml \
-v hugo-theme-branch=$HUGO_THEME_BRANCH \
-v artifact-bucket=$ARTIFACT_BUCKET
