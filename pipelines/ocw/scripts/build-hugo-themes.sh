#!/bin/sh

set -e  # exit on error
set -x  # show commands on stdout

export PATH=$PATH:/usr/local/go/bin
export EXTERNAL_SITE_PATH=../ocw-www

cd ocw-hugo-themes
yarn install --pure-lockfile
npm run build:githash
npm run build

# Just for testing rclone!
mkdir ../ocw-www/dist/courses
date > ../ocw-www/dist/courses/testfile1
date > ../ocw-www/dist/testfile2
