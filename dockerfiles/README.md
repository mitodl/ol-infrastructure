# Dockerfiles

This directory contains Dockerfiles for MIT Open Learning services.

## edX / Open edX

The edX Dockerfiles that previously lived here (`openedx-edxapp`,
`openedx-codejail`, `openedx-forum`, `openedx-notes`, `openedx-xqueue`) have
been removed. All edX image builds are now managed in the
[mitodl/lehrer](https://github.com/mitodl/lehrer) repository.

The Kubernetes deployment configuration for edX applications remains in this
repository under
[`src/ol_infrastructure/applications/edxapp/`](../src/ol_infrastructure/applications/edxapp/).
