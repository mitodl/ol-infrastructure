# Reference

https://github.com/mitodl/rapid-response-xblock/blob/master/README.md

# How-To

1. Common env.yaml file must have `FEATURES:ALLOW_ALL_ADVANCED_COMPONENTS: True`
2. Common env.yaml file must have `ENABLE_RAPID_RESPONSE_AUTHOR_VIEW: True`
3. Rapid-response package pip packages must be installed in the image `rapid-response-xblock` and `ol-openedx-rapid-response-reports`. Refer to existing package listings in the openedx docker configs.
4. In the _LMS_ Admin UI -> `/admin/lms_xblock/xblockasidesconfig/` create an `enabled` record.
5. In the _CMS_ Admin UI -> `/admin/xblock_config/studioconfig/` create an 'enabled' record.
6. Verify via studio on a test/demo course, find an existing multiple choice problem or create a new one. After creation in the 'unit view', you should now have a checkbox at the bottom of a multiple choice problem that will say 'Enable problem for rapid-response'.
