#!/bin/sh

set -e # fail on error
set -x # show commands on stdout

basepath=$(pwd)
cd ocw-www
cp go.mod go.mod.backup
printf "\nreplace github.com/mitodl/ocw-hugo-themes/base-theme => %s/ocw-hugo-themes/base-theme\n" "$basepath" >>go.mod
printf "\nreplace github.com/mitodl/ocw-hugo-themes/www => %s/ocw-hugo-themes/www\n" "$basepath" >>go.mod
