#!/bin/bash
#
# Will reconcile configurations for edxapp_v2 vs edxapp
# There are some filename changes so this is easier
#
#
apps=(mitxonline mitx mitx-staging xpro)

for app_name in "${apps[@]}"; do
  echo ""
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"

  echo "*************************************************"
  echo "diff $app_name lms config"
  echo "*************************************************"
  diff "./$app_name/lms_only.yml.tmpl" "../../../edxapp/templates/edxapp/$app_name/lms_only.yml.tmpl"
  echo "*************************************************"
  read -p "any key to continue" -n 1 -r

  echo "*************************************************"
  echo "diff $app_name cms config"
  echo "*************************************************"
  diff "./$app_name/cms_only.yml.tmpl" "../../../edxapp/templates/edxapp/$app_name/studio_only.yml.tmpl"
  echo "*************************************************"
  read -p "any key to continue" -n 1 -r

  echo "*************************************************"
  echo "diff $app_name common config"
  echo "*************************************************"
  diff "./$app_name/common_values.yml.tmpl" "../../../edxapp/templates/edxapp/$app_name/common_values.yml.tmpl"
  echo "*************************************************"
  read -p "any key to continue" -n 1 -r
done
