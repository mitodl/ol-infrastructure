#!/bin/sh

set -e  # exit on error
set -x  # show commands on stdout

export PATH=$PATH:/usr/local/go/bin
export EXTERNAL_SITE_PATH=../ocw-www
export OCW_TO_HUGO_OUTPUT_DIR=../course-markdown
export COURSE_OUTPUT_DIR=$EXTERNAL_SITE_PATH/dist/courses
export COURSE_BASE_URL=$COURSE_BASE_URL

cd ocw-hugo-themes
yarn install --pure-lockfile
./build_scripts/build_all_courses.sh
