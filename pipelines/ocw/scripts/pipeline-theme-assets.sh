#!/bin/bash

yes | fly -t $FLY_TARGET set-pipeline \
-p ocw-theme-assets-$HUGO_THEME_BRANCH \
--team=ocw \
--config=pipelines/ocw/pipeline-theme-assets.yml \
-v hugo-theme-branch=$HUGO_THEME_BRANCH
