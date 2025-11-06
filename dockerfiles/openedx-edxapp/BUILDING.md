# Building

The Earthfile in this directory is responsible for building the image that we deploy in our various environments.

## Naming Conventions

- `{release_name}` - `olive`, `palm`, etc
- `{deployment_name} - `mitx`, `mitx-staging`, `mitxonline`, or `xpro`

## Arguments

- `RELEASE_NAME` see naming conventions - *required*
- `DEPLOYMENT_NAME` see naming conventions - *required*
- `OPENEDX_I18N_VERSION` branch of the localization reposiltory `openedx-i18n` to include. *optional*
- `TUTOR_REPO` address of the tutor repo clone *optional*
- `TUTOR_BRANCH` branch of the tutor repo to clone *optional*
- `APP_USER_ID` UID of the application user in the container. Set to `1000`, don't alter unless you have a good reason and know that many things in the AMI are set to a UID/GID of `1000` for permissions matching between host and container.

## Fully Functioning Example
`earthly +all --DEPLOYMENT_NAME=mitxonline --RELEASE_NAME=master --EDX_PLATFORM_GIT_REPO=https://github.com/openedx/edx-platform --EDX_PLATFORM_GIT_BRANCH=master --THEME_GIT_REPO=https://github.com/mitodl/mitxonline-theme --THEME_GIT_BRANCH=main --OPENEDX_TRANSLATIONS_BRANCH=main --PYTHON_VERSION=3.11 --NODE_VERSION=24.11.0`
