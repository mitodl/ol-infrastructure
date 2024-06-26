# Summary

This document will serve as a place to put patterns and best practices.

The goal is to ease the path for both new builders and experienced builders by
helping narrow down the bevvy of choices and present a workable best practice
solution to a given problem.

There are many potential ways to organize a document like this, but for now I
intend to start with the recipes broken down by infrastructure components.

Example of what I mean are Docker, Traefik, Pyinfra and the like.

Please write your recipes in the following form with _What I Want_ and _How To
Build It_ as bold subsections.

# Recipes

## Traefik

### Token Based Authentication

_What I Want_

I want Traefik to allow requests only from clients that pass a particular token
in the HTTP headers of the request. Here's an example `curl` from Tika with the
actual token:

```bash
curl  --header 'X-Access-Token: <crazy hex digits>' https://tika-qa.odl.mit.edu
```

_How To Build It_

Traefik does not contain this functionality by default, so we must leverage the
[checkheaders](https://plugins.traefik.io/plugins/628c9eda108ecc83915d7760/check-request-headers-plugin)
Traefik middleware plugin.

You will need to add a blob to your Traefik static configuration like this:

```
experimental:
  plugins:
    checkheadersplugin:
      moduleName: "github.com/dkijkuit/checkheadersplugin"
      version: "v0.2.6"
```

Since we use [pyinfra](https://pyinfra.com/) to automate our image builds,
you'll need to add code like [this](https://github.com/mitodl/ol-infrastructure/blob/b71388a4e0c2d6099ccbe92236301c5c30dc6154/src/bilder/images/tika/deploy.py#L110C1-L110C1)
to your deploy.py and a line like [this](https://github.com/mitodl/ol-infrastructure/blob/b71388a4e0c2d6099ccbe92236301c5c30dc6154/src/bilder/images/tika/files/docker-compose.yaml#L35)
to your docker-compose.yaml file.
