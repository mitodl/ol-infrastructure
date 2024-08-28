# What to add to you `~/.kube/config` file to connect to a new cluster

## Overview
For every cluster, we create a unique IAM Role that allows the devops team to use `kubectl` with the cluster after assuming that role.

Each stack will include in its output a `kube_config_data` structure that contains the important bits you need to add to your kube_config file.

Example Stack Output:
```
    kube_config_data: {
        certificate-authority-data: {
            data: "Base64 Encoded CA for the cluster"
        }
        role_arn                  : "arn:aws:iam::610119931565:role/ol-infrastructure/eks/<cluster_name>/<unique admin role identifier>"
        server                    : "https://<unique Kube-API endpoint for the cluster>.gr7.us-east-1.eks.amazonaws.com"
```
It is generally easiest to use the same file-local name for all three blocks and to use the name of the cluster as that name. To use the `operations-ci` environment as an example: the `context`, `cluster`, and `user` would all named `operations-ci`

# `kube_config` Context Block
```
- name: < file-local name you're fiving this context >
  context:
    cluster: < file-local name you're going to give the cluster configuration >
    user: < file-local name you're going to give the username configuration >
```
# `kube_config` Cluster Block

```
- name: < the file-local name from your context block >
  cluster:
    certificate-authority-data: < kube_config_data.certificate-authority-data >
    server: < kube_config_data.server >
```

# `kube_config` User Block
```
- name: < the file local name from your context block >
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      args:
      - eks
      - get-token
      - --cluster-name
      - operations-ci
      - --role
      - "< kube_config_data.role_arn >"
      command: aws
      env:
      - name: "KUBERNETES_EXEC_INFO"
        value: '{\"apiVersion\": \"client.authentication.k8s.io/v1beta1\"}'
      interactiveMode: IfAvailable
      provideClusterInfo: false
```
