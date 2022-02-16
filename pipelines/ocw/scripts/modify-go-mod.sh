#!/bin/sh

set -e # fail on error
set -x # show commands on stdout

basepath=$(pwd)
cd ocw-www
cp go.mod go.mod.backup
printf "\nreplace github.com/mitodl/ocw-hugo-themes/base-theme => $basepath/ocw-hugo-themes/base-theme\n" >>go.mod
printf "\nreplace github.com/mitodl/ocw-hugo-themes/www => $basepath/ocw-hugo-themes/www\n" >>go.mod
