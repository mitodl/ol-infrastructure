#!/bin/bash
set -euo pipefail


if [ -s optimization_pipeline_output/warnings.txt ]; then
  exit 1
else
  exit 0
fi
