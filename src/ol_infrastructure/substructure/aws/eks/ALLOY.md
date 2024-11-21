# Alloy Documentation

- https://github.com/grafana/alloy/blob/main/operations/helm/charts/alloy/values.yaml
- https://grafana.com/docs/alloy/latest/reference/
- https://relabeler.promlabs.com/  (Helpful with the weird regex engine grafana/prometheus use)

# Discovery and Collection Pipeline

# Discovery and Collection Pipeline

## Pod Logs
1. Discover all pods in the cluster.
2. Relabel all pod metadata in the cluster:
  a. Standarize an `application` label as the namespace of the pod.
  b. Determine a `service` label based on the following pod labels, in order of preference: [`ol.mit.edu/service`,`ol.mit.edu/component`. `app.kubernetes.io/component`, `app.kubernetes.io/name`, `app.kuberentes.io/instance`]
1. Standarize an `environment` labels as `namespace-{ci,qa,production}`.
d. Standarize a `namespaces` label as the namespace of the pod.
  e. Standarize a `container` label as the name of the container the log came from.
  f. Determin a `stack` label based on the following labels, in order of preference:
    1. `ol.mit.edu/stack`
    2. `pulumi_stack`
  g. Lowercase all labels besides `stack`.
  h. Add one final label `cluster`, which is static per cluster.
  i. Discard any labels besides the following: [`application`, `cluster`, `container`, `environment`, `namespace`, `service`, `stack`]
3. Actually collect logs from the various pods and label accordingly per #2.
4. Ship the logs to Grafana.

## Cluster Events
1. Source cluster events via Kube API.
2. Apply static labels to the logs:
  a. `cluster` is the cluster name
  b. `service` is `kubernetes-events`
  c. `application` is `eks`
  d. `environment` is the same as `cluster`.
3. Additionally, a dynamic label `namespace` is applied when applicable.
4. Discard any labels besides those mentioned in #2 and #3.
4. Ship the logs to grafana.
