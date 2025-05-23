# Developer EKS Access

See https://pe.ol.mit.edu/how_to/developer_eks_access/ for instructions on
how to initially set up developer access to EKS and learn about the available
commands.

Once that is complete, you can use the helper bash script `eks.sh` as a shortcut
for regenerating AWS creds, creating/overwriting your `~/.kube/config` file,
setting a context (default is "applications-qa") and optionally listing the
pods available in a namespace.
