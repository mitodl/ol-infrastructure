#!/bin/bash

# This script requires prior authentication via target "odl-concourse"

yes | fly -t odl-concourse set-pipeline \
	-p ocw-to-hugo-production \
	--team=ocw \
	--config=pipelines/ocw/ocw-to-hugo.yml \
	--load-vars-from pipelines/ocw/vars/ocw-to-hugo/production.yml
