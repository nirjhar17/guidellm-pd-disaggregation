#!/bin/bash
# ROSA MachinePool commands for GPU node management
# These are NOT Kubernetes manifests -- they're rosa CLI commands

CLUSTER_ID=$(oc get infrastructure cluster -o jsonpath='{.status.infrastructureName}')

# ── Set fixed 2 GPU nodes (disable autoscaling) ──
rosa edit machinepool gpu \
  --cluster="${CLUSTER_ID}" \
  --replicas=2 \
  --enable-autoscaling=false

# ── Add time-slicing label so new nodes auto-configure ──
rosa edit machinepool gpu \
  --cluster="${CLUSTER_ID}" \
  --labels="nvidia.com/device-plugin.config=Tesla-T4"

# ── Verify ──
rosa list machinepools --cluster="${CLUSTER_ID}"

# Expected output for gpu pool:
#   AUTOSCALING: No
#   REPLICAS: 2/2
#   LABELS: nvidia.com/device-plugin.config=Tesla-T4
#   TAINTS: nvidia.com/gpu=present:NoSchedule
