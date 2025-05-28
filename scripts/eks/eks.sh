#!/bin/bash
set -euo pipefail
###############################################################################
# Helper script to login to an EKS cluster. It accepts the following options:
#
# -c <context>   The Kubernetes context to use (ex: applications-qa, default: none))
# -n <namespace> The Kubernetes namespace to use (ex: learn-ai, default: none)
# -d <duration>  The duration in minutes for which the AWS credentials are valid
# -g Set AWS creds globally (default: false)
#
# After the command completes, run `source eks.env` to set the environment vars
# in your current shell session.
#
# Usage: bash ./eks.sh -c <context> -n <namespace>
# Example: bash ./eks.sh -c applications-qa -n learn-ai
#
# You must first create a github.env file & add your github token value to it.
# You must also have already installed the required dependencies specified
# at https://pe.ol.mit.edu/how_to/developer_eks_access/
###############################################################################

# Initialize variables for context and namespace
KUBE_CONTEXT=""
KUBE_NAMESPACE=""
AWS_EXPIRES_IN="60"
AWS_GLOBAL_CREDS=false
EKS_PATH="../../src/ol_infrastructure/infrastructure/aws/eks"

# Parse command-line options
while getopts ":c:n:d:g" opt; do
  case $opt in
    c)
      KUBE_CONTEXT="$OPTARG"
      ;;
    n)
      KUBE_NAMESPACE="$OPTARG"
      ;;
    d)
      AWS_EXPIRES_IN="$OPTARG"
      ;;
    g)
      AWS_GLOBAL_CREDS=true
      ;;
    \?)
      echo "Invalid option: -$OPTARG" >&2
      exit 1
      ;;
    :)
      echo "Option -$OPTARG requires an argument." >&2
      exit 1
      ;;
  esac
done

shift $((OPTIND -1))


if [ -n "$KUBE_CONTEXT" ]; then
  echo "Using context: $KUBE_CONTEXT"
fi
if [ -n "$KUBE_NAMESPACE" ]; then
  echo "Using namespace: $KUBE_NAMESPACE"
fi

# shellcheck source=/dev/null
source github.env
python "$EKS_PATH/login_helper.py" aws_creds -d "$AWS_EXPIRES_IN" | grep '^export' > eks.env
# shellcheck source=/dev/null
source eks.env
if [ "$AWS_GLOBAL_CREDS" == true ]; then
  echo "Setting global AWS credentials globally"
  aws configure set aws_access_key_id "$AWS_ACCESS_KEY_ID"
  aws configure set aws_secret_access_key "$AWS_SECRET_ACCESS_KEY"
  aws configure set aws_region  "$AWS_REGION"
  aws configure set aws_default_region "$AWS_DEFAULT_REGION"
fi

python "$EKS_PATH/login_helper.py" kubeconfig > ~/.kube/config

if [ -n "$KUBE_CONTEXT" ]; then
    kubectl config use-context "$KUBE_CONTEXT"
fi
if [ -n "$KUBE_NAMESPACE" ]; then
  kubectl get pods -n "$KUBE_NAMESPACE"
fi
