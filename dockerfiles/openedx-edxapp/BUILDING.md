# Building

This dockerfile is intended to be built by concourse and you'll have an unpleasant time trying to build it locally. It will probably be doubly unpleasant if you're trying build on Apple Silicon.

## Naming Conventions

- `{release_name}` - `olive`, `palm`, etc
- `{deployment_name} - `mitx`, `mitx-staging`, `mitxonline`, or `xpro`

## Exptected Directories

- `./edx_platform` - Full checkout of edx-platform repo on the desired branch.
- `./collected_themes/{deployment_name}` - Full checkout of theme for the deployment

## Arguments

- `RELEASE_NAME` see naming conventions - *required*
- `DEPLOYMENT_NAME` see naming conventions - *required*
- `OPENEDX_I18N_VERSION` branch of the localization reposiltory `openedx-i18n` to include. *optional*
- `TUTOR_REPO` address of the tutor repo clone *optional*
- `TUTOR_BRANCH` branch of the tutor repo to clone *optional*
- `APP_USER_ID` UID of the application user in the container. Set to `1000`, don't alter unless you have a good reason and know that many things in the AMI are set to a UID/GID of `1000` for permissions matching between host and container.

## Other items

- The build context is this directory.
- The build target is `final`
