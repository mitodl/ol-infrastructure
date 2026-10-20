## tika servers

There is nothing particularly special about [tika](https://tika.apache.org/) servers and their environments.

- They require a `x_access_token` specified in the sops configurations for each environment.
  - There are application consumers of the tika service in QA + Production, so you should set this to the right thing found in vault.
- They run on the applications EKS cluster via the upstream Helm chart. CPU/memory and the JVM heap (`JAVA_TOOL_OPTIONS`) are set in `__main__.py`, with the reasoning in the comments there.

There isn't much else worth remarking on for Tika
