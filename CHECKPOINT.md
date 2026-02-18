# P/D Disaggregation Lab -- Knowledge Checkpoint

**Date**: February 17, 2026
**Cluster**: ROSA HCP 4.20.6 (AWS ap-southeast-1)
**Model**: Qwen/Qwen3-0.6B on llm-d + KServe
**GPU Nodes**: 2x g4dn.xlarge (NVIDIA Tesla T4, 16 GiB VRAM each)

---

## What We Achieved

Configured Prefill/Decode (P/D) disaggregation on llm-d with:
- 2 prefill pods + 2 decode pods
- GPU time-slicing (4 virtual GPUs per physical T4)
- 2 pods per GPU node (40% VRAM each)

---

## Architecture

```
                     User Request
                          |
                          v
                  ┌──────────────┐
                  │   Router /   │
                  │  Scheduler   │
                  └──────┬───────┘
                         |
              ┌──────────┴──────────┐
              v                     v
    ┌──────────────┐      ┌──────────────┐
    │  PREFILL Pod │      │  PREFILL Pod │
    │  (node 1)    │      │  (node 1)    │
    └──────┬───────┘      └──────┬───────┘
           |  KV cache transfer  |
           v                     v
    ┌──────────────┐      ┌──────────────┐
    │  DECODE Pod  │      │  DECODE Pod  │
    │  (node 2)    │      │  (node 2)    │
    └──────────────┘      └──────────────┘
```

### How P/D Works
1. **Prefill pod** receives the full prompt, processes all tokens in parallel, builds the KV cache
2. KV cache is **transferred** to the decode pod
3. **Decode pod** uses the KV cache to generate tokens one at a time (auto-regressive)
4. The **router/scheduler** intelligently routes requests using scoring plugins (queue, kv-cache-utilization, prefix-cache)

---

## Key Configurations

### LLMInferenceService Spec (Final Working Config)

```yaml
spec:
  replicas: 2                    # decode pod count
  prefill:
    replicas: 2                  # prefill pod count
  model:
    name: Qwen/Qwen3-0.6B
    uri: hf://Qwen/Qwen3-0.6B
```

### vLLM Args (Critical for Time-Slicing)

```
--dtype=half
--max-model-len=2048
--max-num-seqs=64
--gpu-memory-utilization=0.40
--enforce-eager
--enable-auto-tool-choice
--tool-call-parser hermes
```

### Resource Requests/Limits (GPU must match)

```yaml
resources:
  requests:
    cpu: "1"
    memory: 4Gi
    nvidia.com/gpu: "1"
  limits:
    cpu: "2"
    memory: 6Gi
    nvidia.com/gpu: "1"    # MUST equal request for GPUs
```

---

## GPU Time-Slicing Setup

### What It Does
Exposes 1 physical GPU as 4 virtual GPU slots in Kubernetes. Does NOT partition VRAM -- pods share the full 16 GiB.

### ConfigMap (nvidia-gpu-operator namespace)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: device-plugin-config
  namespace: nvidia-gpu-operator
data:
  Tesla-T4: |-
    version: v1
    sharing:
      timeSlicing:
        resources:
        - name: nvidia.com/gpu
          replicas: 4
```

### ClusterPolicy Reference

```yaml
spec:
  devicePlugin:
    config:
      name: device-plugin-config
      default: ""              # Empty = needs node label to activate
```

### Node Label Required

```
nvidia.com/device-plugin.config=Tesla-T4
```

### ROSA MachinePool (Permanent Fix)

```bash
rosa edit machinepool gpu \
  --cluster=<cluster-id> \
  --labels="nvidia.com/device-plugin.config=Tesla-T4"
```

This ensures every GPU node automatically gets the label. Without this, new nodes come up with GPU: 1 instead of GPU: 4.

---

## Lessons Learned (Troubleshooting Chain)

### 1. GPU Request Must Equal Limit
**Error**: `nvidia.com/gpu requests: Invalid value: "1": must be equal to nvidia.com/gpu limit of 2`
**Why**: NVIDIA GPUs can't be overcommitted. Kubernetes enforces request == limit.
**Fix**: Always set both to the same value.

### 2. Merge Patches Can Clobber Fields
**Problem**: Using `oc patch --type=merge` on containers array replaces the entire container spec, dropping fields like `resources` or `image`.
**Fix**: Use `oc replace -f -` with the full object for complex changes, or include ALL fields in every merge patch.

### 3. GPU Memory Utilization vs Kubernetes Memory
Two completely different things:
- `resources.requests.memory: 4Gi` = CPU/system RAM for scheduling
- `--gpu-memory-utilization=0.40` = vLLM flag controlling how much GPU VRAM to use

### 4. Time-Slicing Does NOT Partition VRAM
With time-slicing (4 replicas), Kubernetes sees 4 GPU slots. But ALL pods sharing a GPU see the FULL 16 GiB VRAM. If two pods each set `--gpu-memory-utilization=0.85`, the second one OOMs.

**Math for 2 pods per GPU**:
```
40% + 40% = 80% of 16 GiB = 12.8 GiB   (fits)
85% + 85% = 170% of 16 GiB = 27.2 GiB  (OOM!)
```

### 5. max-model-len and max-num-seqs Affect VRAM
Even with `--gpu-memory-utilization=0.40`, vLLM may exceed it if the KV cache requirements (driven by max-model-len x max-num-seqs) are too large.

**Error**: `CUDA out of memory occurred when warming up sampler with 256 dummy requests`
**Fix**: Lowered `--max-model-len` from 4096 to 2048 and `--max-num-seqs` from 256 to 64.

### 6. Model Weight Size = Parameters x Bytes per Dtype
```
Qwen3-0.6B in float16: 0.6B x 2 bytes = 1.12 GiB
```
vLLM confirms: `Model loading took 1.1201 GiB memory`

### 7. Cluster Autoscaler Can Remove Nodes Mid-Operation
When we deleted deployments to recreate them, GPU nodes became idle. The autoscaler scaled them down before new pods arrived.
**Fix**: Set fixed replicas in ROSA machinepool (disable autoscaling) or annotate nodes with `cluster-autoscaler.kubernetes.io/scale-down-disabled=true`.

### 8. New Nodes Need Time-Slicing Label
Every new GPU node from ROSA comes bare -- no time-slicing label.
**Fix**: Add the label to the ROSA machinepool definition so it's inherited by all new nodes automatically.

### 9. Device Plugin Restart After Labeling
After adding the time-slicing label to a node, the NVIDIA device plugin pod must be restarted to pick up the new config.
```bash
oc delete pod <nvidia-device-plugin-daemonset-xxx> -n nvidia-gpu-operator
```

### 10. Image Pull Cold Start on New Nodes
The vLLM CUDA image is ~8-10 GB. First pull on a new node takes 5-10 minutes. Cached nodes start pods in ~2 minutes.
**Mitigation**: PinnedImageSets (OpenShift 4.20+), DaemonSet pre-puller, or fixed node count.

---

## Pod Lifecycle Phases (Reference)

1. **Pending** -- scheduling + image pulling
2. **Init Containers** -- storage-initializer (downloads model from HF), llm-d-routing-sidecar (starts proxy)
3. **Running (Not Ready)** -- vLLM loading model weights, torch.compile, CUDA graph capture
4. **Running (Ready)** -- passing health checks, serving traffic
5. **Terminating** -- draining requests, shutting down

---

## Commands Cheat Sheet

```bash
# Check P/D pod distribution
oc get pods -n my-first-model -o custom-columns='NAME:.metadata.name,ROLE:.metadata.labels.llm-d\.ai/role,READY:.status.conditions[?(@.type=="Ready")].status,NODE:.spec.nodeName'

# Check GPU allocatable per node
oc get nodes -l nvidia.com/gpu.present=true -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'

# Check GPU memory usage across cluster
oc get pods --all-namespaces -o json | python3 -c "
import json,sys
data=json.load(sys.stdin)
for pod in data['items']:
    if pod['status']['phase'] in ('Running','Pending'):
        for c in pod['spec'].get('containers',[]):
            gpu = c.get('resources',{}).get('requests',{}).get('nvidia.com/gpu','0')
            if gpu != '0':
                ns = pod['metadata']['namespace']
                name = pod['metadata']['name']
                node = pod['spec'].get('nodeName','unscheduled')
                print(f'{ns}/{name}  GPU:{gpu}  Node:{node}')
"

# Check LLMInferenceService status
oc get llminferenceservice qwen3-0-6b -n my-first-model -o jsonpath='{range .status.conditions[*]}{.type}: {.status} - {.message}{"\n"}{end}'

# Check vLLM logs for a pod
oc logs <pod-name> -n my-first-model -c main --tail=20

# Check ROSA GPU machinepool
rosa list machinepools --cluster=$(oc get infrastructure cluster -o jsonpath='{.status.infrastructureName}')

# Scale P/D replicas
oc get llminferenceservice qwen3-0-6b -n my-first-model -o json | python3 -c "
import json,sys
obj = json.load(sys.stdin)
obj['spec']['replicas'] = 2
obj['spec']['prefill']['replicas'] = 2
print(json.dumps(obj))
" | oc replace -f -

# Restart device plugin after labeling a node
oc delete pod -n nvidia-gpu-operator -l app=nvidia-device-plugin-daemonset --field-selector spec.nodeName=<node-name>
```

---

## Current State (as of checkpoint)

| Component | Status |
|-----------|--------|
| Prefill pods | 2x Ready, 0 restarts |
| Decode pods | 2x Ready, 0 restarts |
| Router/Scheduler | Ready |
| GPU Node 1 (134-199) | 2 prefill pods, GPU: 4 (time-sliced) |
| GPU Node 2 (135-252) | 2 decode pods, GPU: 4 (time-sliced) |
| MachinePool label | nvidia.com/device-plugin.config=Tesla-T4 (permanent) |
| gpu-memory-utilization | 0.40 per pod |
| max-model-len | 2048 |
| max-num-seqs | 64 |

---

## Manifests (in ./manifests/)

| File | Description |
|------|-------------|
| `01-llminferenceservice-pd.yaml` | Full LLMInferenceService with P/D disaggregation (2 prefill + 2 decode) |
| `02-gpu-timeslicing-configmap.yaml` | NVIDIA device plugin ConfigMap for 4x time-slicing on Tesla T4 |
| `03-clusterpolicy-patch.yaml` | Patch for ClusterPolicy to reference the time-slicing ConfigMap |
| `04-rosa-machinepool-commands.sh` | ROSA CLI commands for fixed GPU nodes + auto-labeling |

### Apply order:
```bash
# 1. Create the time-slicing ConfigMap
oc apply -f manifests/02-gpu-timeslicing-configmap.yaml

# 2. Patch the ClusterPolicy
oc patch clusterpolicy gpu-cluster-policy --type=merge -p "$(cat manifests/03-clusterpolicy-patch.yaml)"

# 3. Configure ROSA machinepool (run the commands in the script)
bash manifests/04-rosa-machinepool-commands.sh

# 4. Wait for GPU nodes to show GPU: 4, then apply the LLMInferenceService
oc apply -f manifests/01-llminferenceservice-pd.yaml
```
