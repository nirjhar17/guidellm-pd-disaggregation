# GuideLLM Benchmarking with P/D Disaggregation on OpenShift AI

Hands-on benchmarking of Prefill/Decode disaggregation on OpenShift AI using GuideLLM and inference-perf, with EPP intelligent routing, prefix caching, and full EPP troubleshooting.

## What's Here

- **Blog**: [Benchmarking P/D Disaggregation on OpenShift AI with GuideLLM](https://nirjhar17.github.io/guidellm-pd-disaggregation/blog-benchmarking-llm-d-guidellm)
- **EPP Troubleshooting Guide**: [llm-d-request-flow-guide.md](llm-d-request-flow-guide.md) — full request flow, 4 bugs found and fixed, debugging commands
- **Manifests**: Kubernetes YAMLs for deploying the full P/D disaggregation stack
- **Reports (v2)**: GuideLLM benchmark results with EPP active (JSON + HTML) in `reports-v2/`
- **Reports (v1)**: Original results before EPP fix (round-robin) in `reports/`
- **Checkpoints**: CHECKPOINT.md, CHECKPOINT-2.md, CHECKPOINT-5.md — learning journey notes

## Environment

- ROSA HCP 4.20.6 (AWS ap-southeast-1)
- OpenShift AI (RHOAI 3.2) with llm-d
- Istio 1.26.2 (Service Mesh 3)
- Model: Qwen/Qwen3-0.6B (vLLM v0.11.2)
- GPU: 2x NVIDIA Tesla T4 (16 GiB VRAM each)
- GPU time-slicing: 4 virtual slots per physical GPU
- Pods: 2 prefill + 2 decode + 1 EPP scheduler

## Manifests

| File | Description |
|------|-------------|
| `manifests/01-llminferenceservice-pd.yaml` | LLMInferenceService with P/D disaggregation (2 prefill + 2 decode + EPP) |
| `manifests/02-gpu-timeslicing-configmap.yaml` | NVIDIA device plugin ConfigMap for 4x time-slicing on Tesla T4 |
| `manifests/03-clusterpolicy-patch.yaml` | Patch for ClusterPolicy to reference the time-slicing ConfigMap |
| `manifests/04-rosa-machinepool-commands.sh` | ROSA CLI commands for fixed GPU nodes + auto-labeling |
| `manifests/05-benchmark-profiles.yaml` | All 7 GuideLLM Job definitions |
| `manifests/06-inference-perf-shared-prefix-config.yml` | inference-perf config for shared prefix workload |
| `manifests/07-inference-perf-job.yaml` | Kubernetes Job for inference-perf |
| `manifests/08-envoyfilter-epp.yaml` | EnvoyFilter to connect Envoy Gateway to EPP (required on RHOAI 3.0-3.2) |

## Deploy from Scratch

Prerequisites: OpenShift cluster with RHOAI installed, GPU nodes available, `oc` logged in.

```bash
# 1. GPU time-slicing (skip if GPUs already have enough slots)
oc apply -f manifests/02-gpu-timeslicing-configmap.yaml
oc patch clusterpolicy gpu-cluster-policy --type=merge \
  -p "$(cat manifests/03-clusterpolicy-patch.yaml)"
bash manifests/04-rosa-machinepool-commands.sh

# 2. Wait for GPU nodes to show nvidia.com/gpu: 4
oc get nodes -l nvidia.com/device-plugin.config=Tesla-T4 \
  -o jsonpath='{range .items[*]}{.metadata.name}: {.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'

# 3. Deploy the model with P/D disaggregation
oc apply -f manifests/01-llminferenceservice-pd.yaml

# 4. Wait for all pods to be Ready (2 decode + 2 prefill + 1 EPP)
oc get pods -n my-first-model -w

# 5. Apply the EnvoyFilter (REQUIRED on RHOAI 3.0-3.2)
#    Without this, EPP is dead and all requests go through round-robin.
#    Update the cluster_name in the YAML if your model name differs from qwen3-0-6b.
oc apply -f manifests/08-envoyfilter-epp.yaml

# 6. Verify EPP is connected (cx_total > 0, cx_connect_fail = 0)
ENVOY_POD=$(oc get pods -n openshift-ingress \
  -l gateway.networking.k8s.io/gateway-name=openshift-ai-inference \
  -o jsonpath='{.items[0].metadata.name}')
oc exec -n openshift-ingress $ENVOY_POD -c istio-proxy -- \
  pilot-agent request GET /clusters | grep "epp-service.*cx_total"

# 7. Quick test
GW=$(oc get gateway openshift-ai-inference -n openshift-ingress \
  -o jsonpath='{.status.addresses[0].value}')
curl -sk -X POST "https://$GW/my-first-model/qwen3-0-6b/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hi"}],"max_tokens":5}'

# 8. Run GuideLLM benchmarks
oc create ns guidellm-lab 2>/dev/null
# Create a PVC for results first, then:
oc apply -f manifests/05-benchmark-profiles.yaml

# 9. Run inference-perf (shared prefix workload)
oc create configmap inference-perf-config \
  --from-file=manifests/06-inference-perf-shared-prefix-config.yml \
  -n guidellm-lab
oc apply -f manifests/07-inference-perf-job.yaml
```

## Benchmark Results (with EPP active)

7 load profiles tested, each for 60 seconds with prompt_tokens=256 and output_tokens=128:

| Profile | Requests | RPS | TTFT Median | ITL Median |
|---------|----------|-----|-------------|------------|
| Synchronous | 22 | 0.35 | 96.6ms | 20.4ms |
| Concurrent (4) | 78 | 1.28 | 97.6ms | 23.3ms |
| Concurrent (16) | 278 | 4.62 | 108.6ms | 26.1ms |
| Throughput (64) | 738 | 12.28 | 183.3ms | 39.0ms |
| Constant (5/s) | 282 | 4.70 | 102.5ms | 26.6ms |
| Poisson (5/s) | 337 | 5.60 | 124.1ms | 29.3ms |
| Sweep (auto) | 22–902 | 0.35–14.93 | 92.4–161.9ms | 21.7–47.5ms |

Prefix cache (inference-perf, 100 shared-prefix requests): **86% hit rate**, decode pod distribution **100/0** (session affinity).

### Using the Reports

```bash
# View new results (EPP active)
python3 reports/parse_benchmarks.py reports-v2/

# View old results (before EPP fix, round-robin)
python3 reports/parse_benchmarks.py reports/

# Export as CSV
python3 reports/parse_benchmarks.py reports-v2/ --csv > results.csv
```

## EPP Issues on RHOAI 3.0–3.2

We found 4 bugs that prevent EPP intelligent routing from working out of the box. All must be fixed:

1. **Dummy ext_proc cluster** — EnvoyFilter needed to replace `dummy`/`SKIP` with real EPP
2. **TLS cert-path missing** — Add `--cert-path=/var/run/kserve/tls` and `--secure-serving` to EPP args
3. **Wrong EndpointPickerConfig** — Use `pd-profile-handler` with separate prefill/decode profiles
4. **Prefix caching disabled** — Add `--enable-prefix-caching --block-size=16` to vLLM args

The LLMInferenceService YAML in `manifests/01-llminferenceservice-pd.yaml` already includes fixes 2-4. Fix 1 (EnvoyFilter) must be applied separately (step 5 above).

Full details: [llm-d-request-flow-guide.md](llm-d-request-flow-guide.md) | [CHECKPOINT-5.md](CHECKPOINT-5.md)

## Related

- [Observability Stack for llm-d on OpenShift AI](https://github.com/nirjhar17/llm-d-observability-openshift) — Grafana dashboards, EPP troubleshooting guide, full monitoring setup
- [GuideLLM](https://github.com/vllm-project/guidellm) — LLM benchmarking tool by the vLLM team
- [inference-perf](https://github.com/kubernetes-sigs/inference-perf) — Shared prefix benchmarking tool
- [llm-d](https://github.com/llm-d/llm-d) — Kubernetes-native distributed LLM inference

## Author

Nirjhar Jajodia

- GitHub: [github.com/nirjhar17](https://github.com/nirjhar17)
- LinkedIn: [linkedin.com/in/nirjhar-jajodia](https://linkedin.com/in/nirjhar-jajodia)
