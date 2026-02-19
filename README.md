# GuideLLM Benchmarking with P/D Disaggregation on OpenShift AI

Hands-on benchmarking of Prefill/Decode disaggregation on OpenShift AI using GuideLLM, with EPP intelligent routing proof and performance analysis.

## What's Here

- **Blog**: [Benchmarking P/D Disaggregation on OpenShift AI with GuideLLM](https://nirjhar17.github.io/guidellm-pd-disaggregation/blog-benchmarking-llm-d-guidellm)
- **Manifests**: Kubernetes YAMLs for deploying the full P/D disaggregation stack
- **Reports**: GuideLLM benchmark results across 7 load profiles (JSON, HTML, CSV, YAML)
- **Tools**: `parse_benchmarks.py` for extracting metrics from GuideLLM JSON output

## Architecture

```
                     User Request
                          |
                          v
                  ┌──────────────┐
                  │  EPP Router  │
                  │  /Scheduler  │
                  └──────┬───────┘
                         |
              ┌──────────┴──────────┐
              v                     v
    ┌──────────────┐      ┌──────────────┐
    │  PREFILL Pod │      │  PREFILL Pod │
    │  (GPU node 1)│      │  (GPU node 1)│
    └──────┬───────┘      └──────┬───────┘
           |  KV cache transfer  |
           v                     v
    ┌──────────────┐      ┌──────────────┐
    │  DECODE Pod  │      │  DECODE Pod  │
    │  (GPU node 2)│      │  (GPU node 2)│
    └──────────────┘      └──────────────┘
```

## Environment

- ROSA HCP 4.20.6 (AWS ap-southeast-1)
- OpenShift AI with llm-d
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

### Apply order

```bash
# 1. Create the time-slicing ConfigMap
oc apply -f manifests/02-gpu-timeslicing-configmap.yaml

# 2. Patch the ClusterPolicy
oc patch clusterpolicy gpu-cluster-policy --type=merge \
  -p "$(cat manifests/03-clusterpolicy-patch.yaml)"

# 3. Configure ROSA machinepool (run the commands in the script)
bash manifests/04-rosa-machinepool-commands.sh

# 4. Wait for GPU nodes to show GPU: 4, then deploy the model
oc apply -f manifests/01-llminferenceservice-pd.yaml

# 5. Run benchmarks (after all pods are Ready)
oc apply -f manifests/05-benchmark-profiles.yaml
```

## Benchmark Results

7 load profiles tested, each for 60 seconds with prompt_tokens=256 and output_tokens=128:

- Synchronous: 22 requests, 0.35 RPS, TTFT median 64.2ms
- Concurrent (4): 79 requests, 1.30 RPS, TTFT median 54.2ms
- Concurrent (16): 259 requests, 4.30 RPS, TTFT median 75.0ms
- Throughput (64): 758 requests, 12.63 RPS, TTFT median 118.3ms
- Constant (5/s): 281 requests, 4.68 RPS, TTFT median 75.2ms
- Poisson (5/s): 335 requests, 5.58 RPS, TTFT median 85.9ms
- Sweep (auto): 23-1081 requests, 0.37-15.58 RPS, TTFT median 63.3-134.6ms

### Using the Reports

```bash
# View all metrics in terminal
python3 reports/parse_benchmarks.py

# Export as CSV for spreadsheets
python3 reports/parse_benchmarks.py --csv > results.csv
```

## Related

- [Observability Stack for llm-d on OpenShift AI](https://github.com/nirjhar17/llm-d-observability-openshift) -- Grafana dashboards, EnvoyFilter fix, and full monitoring setup
- [GuideLLM](https://github.com/vllm-project/guidellm) -- LLM benchmarking tool by the vLLM team
- [llm-d](https://github.com/llm-d/llm-d) -- Kubernetes-native distributed LLM inference

## Author

Nirjhar Jajodia

- GitHub: [github.com/nirjhar17](https://github.com/nirjhar17)
- LinkedIn: [linkedin.com/in/nirjhar-jajodia](https://linkedin.com/in/nirjhar-jajodia)
