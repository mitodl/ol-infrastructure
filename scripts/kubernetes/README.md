# Kubernetes Cluster Analysis Scripts

Utilities for analyzing Kubernetes cluster resource usage and optimization.

## analyze_cluster_resources.py

Comprehensive script that analyzes a Kubernetes cluster's resource requirements and recommends optimal baseline node configurations.

### Features

- **Workload Analysis**: Enumerates all stable workloads (Deployments, StatefulSets, DaemonSets)
- **Resource Aggregation**: Collects CPU and memory requests/limits across all workloads
- **Nodegroup Introspection**: Analyzes current nodegroup configuration and capacity
- **Instance Type Optimization**: Uses AWS EC2 instance type data to recommend efficient node configurations
- **Cost Estimation**: Provides on-demand pricing for each configuration (hourly, monthly, yearly)
- **High Availability**: Enforces a minimum of 3 nodes for fault tolerance and maintenance windows
- **Fewer Larger Nodes**: Biases recommendations toward fewer, larger instances for operational efficiency
- **Bin Packing**: Calculates optimal node counts to minimize resource waste while maintaining headroom for autoscaling
- **Multiple Output Formats**: Human-readable reports or JSON output for programmatic use

### Requirements

```bash
# System requirements
- kubectl configured and authenticated to cluster
- AWS CLI configured with credentials
- Python 3.9+
- boto3 library
- kubernetes Python client library
```

### Installation

The script uses the repository's standard dependencies. Install with:

```bash
uv sync
```

### Usage

#### Basic Usage

```bash
uv run scripts/kubernetes/analyze_cluster_resources.py my-cluster us-east-1
```

#### Advanced Options

```bash
# Using specific kubeconfig and context
uv run scripts/kubernetes/analyze_cluster_resources.py \
  --cluster-name my-cluster \
  --region us-east-1 \
  --kubeconfig ~/.kube/my-config \
  --context my-context

# Show detailed per-workload breakdown
uv run scripts/kubernetes/analyze_cluster_resources.py \
  my-cluster us-east-1 \
  --detailed

# Adjust headroom for autoscaling (default 20%)
uv run scripts/kubernetes/analyze_cluster_resources.py \
  my-cluster us-east-1 \
  --headroom 30

# Output as JSON for automation
uv run scripts/kubernetes/analyze_cluster_resources.py \
  my-cluster us-east-1 \
  --json-output | jq '.recommendation.recommended'

# Display help
uv run scripts/kubernetes/analyze_cluster_resources.py --help
```

### Output Examples

#### Human-Readable Report

```
================================================================================
KUBERNETES CLUSTER RESOURCE ANALYSIS
================================================================================

### WORKLOAD SUMMARY ###

Total workloads: 42

Aggregated Requests:
  cpu_cores: 12.500
  cpu_millicores: 12500m
  memory_bytes: 53687091200
  memory_gb: 50.00 GB

Aggregated Limits:
  cpu_cores: 25.000
  cpu_millicores: 25000m
  memory_bytes: 107374182400
  memory_gb: 100.00 GB

### WORKLOAD BREAKDOWN ###

Deployment:
  CPU: 10.500 cores (10500m)
  Memory: 45.00 GB

DaemonSet:
  CPU: 1.000 cores (1000m)
  Memory: 3.00 GB

StatefulSet:
  CPU: 1.000 cores (1000m)
  Memory: 2.00 GB

### CURRENT CLUSTER CAPACITY ###

NodeGroups:

  primary-nodegroup:
    Instance Type: t3.xlarge
    Current Nodes: 3
    Desired Capacity: 3
    Min/Max: 1/10
    Total CPU: 12.0 cores
    Total Memory: 45.00 GB

Total Capacity (across all nodegroups):
  CPU: 12.0 cores
  Memory: 45.00 GB

Estimated CPU Utilization: 104.2%
Estimated Memory Utilization: 111.1%

### BASELINE NODE RECOMMENDATION ###

Recommended Instance Type: m5a.2xlarge
  Instance Spec: 8 vCPU, 32 GB RAM
Recommended Node Count: 3
Estimated Resource Waste: 16.3%

Required Resources (with 20% headroom):
  CPU: 18.1 cores
  Memory: 96.03 GB

### ESTIMATED COSTS ###

Recommended Configuration:
  Hourly Cost:  $      0.53
  Monthly Cost: $    386.90 (~730 hours/month)
  Yearly Cost:  $   4,642.80

### TOP 5 ALTERNATIVE CONFIGURATIONS ###

1. m5a.2xlarge (8C 32GB) x 3
   Total Capacity: 24.0 CPU, 96.0 GB RAM
   Resource Waste: 16.3%
   Costs: $0.53/hr, $386.90/mo, $4,642.80/yr

2. m5zn.2xlarge (8C 32GB) x 3
   Total Capacity: 24.0 CPU, 96.0 GB RAM
   Resource Waste: 16.3%
   Costs: $0.60/hr, $438.00/mo, $5,256.00/yr

3. m6id.2xlarge (8C 32GB) x 3
   Total Capacity: 24.0 CPU, 96.0 GB RAM
   Resource Waste: 16.3%
   Costs: $0.69/hr, $503.70/mo, $6,044.40/yr

4. m6a.2xlarge (8C 32GB) x 3
   Total Capacity: 24.0 CPU, 96.0 GB RAM
   Resource Waste: 16.3%
   Costs: $0.65/hr, $474.50/mo, $5,694.00/yr

5. m5dn.2xlarge (8C 32GB) x 3
   Total Capacity: 24.0 CPU, 96.0 GB RAM
   Resource Waste: 16.3%
   Costs: $0.73/hr, $533.90/mo, $6,406.80/yr

================================================================================
```

#### JSON Output

```bash
$ uv run scripts/kubernetes/analyze_cluster_resources.py \
  my-cluster us-east-1 \
  --json-output
```

Output:
```json
{
  "cluster": "my-cluster",
  "region": "us-east-1",
  "workload_count": 42,
  "total_cpu_cores": 12.5,
  "total_memory_gb": 50.0,
  "nodegroups": [
    {
      "name": "primary-nodegroup",
      "instance_type": "t3.xlarge",
      "node_count": 3,
      "capacity": {
        "cpu_cores": 12.0,
        "memory_gb": 45.0
      }
    }
  ],
  "recommendation": {
    "recommended": {
      "instance_type": "m6i.2xlarge",
      "node_count": 1,
      "avg_waste_percent": 8.3
    },
    "top_alternatives": [
      {
        "instance_type": "m6i.2xlarge",
        "node_count": 1,
        "total_cpu": 32.0,
        "total_memory_gb": 64.0,
        "cpu_waste_percent": 4.2,
        "memory_waste_percent": 12.5,
        "avg_waste_percent": 8.3
      }
    ]
  }
}
```

### Understanding the Recommendations

The script recommends node configurations based on:

1. **Resource Requirements**: Total CPU and memory requested by all workloads
2. **Headroom**: Adds a buffer (default 20%) for autoscaled resources and operational overhead
3. **High Availability**: Enforces a minimum of 3 nodes for fault tolerance (survive node failure + maintenance)
4. **Fewer, Larger Nodes**: Biases toward larger instance types to minimize operational complexity and management overhead
5. **Bin Packing Efficiency**: Minimizes wasted resources while maintaining cluster reliability
6. **Cost Estimation**: Shows on-demand pricing for all configurations to aid cost planning
7. **Instance Type Selection**: Prefers general-purpose instances (t3, m5, m6, m7, c5, c6, c7) for balanced workloads

### Key Metrics

- **CPU Utilization**: Percentage of available CPU being requested by workloads
- **Memory Utilization**: Percentage of available memory being requested by workloads
- **Resource Waste**: Percentage of over-provisioned resources in the recommended configuration
- **Headroom**: Buffer (20% default) for autoscaling and unexpected spikes

### Example Workflow

1. **Analyze current cluster:**
   ```bash
   uv run scripts/kubernetes/analyze_cluster_resources.py \
     prod-cluster us-east-1 \
     --detailed > cluster_analysis.txt
   ```

2. **Export data for analysis:**
   ```bash
   uv run scripts/kubernetes/analyze_cluster_resources.py \
     prod-cluster us-east-1 \
     --json-output > cluster_metrics.json
   ```

3. **Review recommendations** in output and decide on baseline node configuration

4. **Apply configuration** via EKS or Pulumi infrastructure code

### Cost Estimation

Cost estimates are calculated using two sources (in order of preference):

1. **AWS Pricing API** - Real-time on-demand pricing from AWS
   - Requires: AWS CLI credentials configured
   - Coverage: All general-purpose instance types in supported regions
   - Accuracy: Official AWS pricing data

2. **Fallback: instances.vantage.sh** - Community-curated pricing
   - Automatic fallback if AWS API unavailable
   - Coverage: All AWS regions and instance types
   - Accuracy: Near real-time, updated regularly

**Note**: If neither source provides pricing, cost estimates will show $0.00. Check stderr output to see which source succeeded.

### Limitations

- DaemonSet replicas counted as 1 per pod (actual count depends on node count)
- Does not account for system pods or kubelet overhead (typically 5-10%)
- Recommendations assume general-purpose workloads; GPU or high-memory workloads may need different instance types
- Does not analyze node affinity, taints, or tolerations
- Cost estimates require either AWS credentials or internet access to instances.vantage.sh

### Troubleshooting

**Error: "Error loading Kubernetes config"**
- Ensure `kubectl` is configured: `kubectl config view`
- Use `--kubeconfig` flag to specify custom config path

**Error: "Error connecting to AWS EKS"**
- Verify AWS credentials: `aws sts get-caller-identity`
- Check IAM permissions for EC2 and EKS APIs

**High utilization (>100%)**
- Current cluster is under-provisioned
- Immediate node addition recommended
- Check for pending pods: `kubectl get pods --field-selector=status.phase=Pending`

**Mismatch between reported and actual capacity**
- Some nodes may have taints/labels preventing workload scheduling
- Check node selectors in workload specs: `kubectl describe node <node-name>`
- Review EKS nodegroup configuration in AWS console

### Integration with Infrastructure-as-Code

Export recommendations to use in Pulumi or Terraform:

```bash
# Get recommended instance type
INSTANCE_TYPE=$(uv run scripts/kubernetes/analyze_cluster_resources.py \
  prod-cluster us-east-1 \
  --json-output | jq -r '.recommendation.recommended.instance_type')

NODE_COUNT=$(uv run scripts/kubernetes/analyze_cluster_resources.py \
  prod-cluster us-east-1 \
  --json-output | jq -r '.recommendation.recommended.node_count')

echo "Update your Pulumi/Terraform with:"
echo "  instance_type = '$INSTANCE_TYPE'"
echo "  desired_size = $NODE_COUNT"
```

### Contributing

Improvements welcome:
- Add cost optimization recommendations
- Support for GPU/memory-optimized instance types
- Network bandwidth analysis
- Storage (EBS/EFS) requirement analysis
- Multi-zone distribution optimization
