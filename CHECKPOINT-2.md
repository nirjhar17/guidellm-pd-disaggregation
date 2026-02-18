# Checkpoint 2 -- GuideLLM Benchmarks + Observability Cleanup

**Date**: February 18, 2026
**Cluster**: ROSA HCP 4.20.6 (AWS ap-southeast-1)
**Model**: Qwen/Qwen3-0.6B on llm-d + KServe (P/D disaggregation)
**Pods**: 2 prefill + 2 decode + 1 EPP scheduler

---

## What We Did in This Session

### 1. Ran All 7 GuideLLM Load Profiles

Ran sequential benchmark Jobs through the EPP Gateway, each for 60 seconds, targeting `Qwen/Qwen3-0.6B` with `prompt_tokens=256, output_tokens=128`.

| # | Profile | Req/Sec (Mean) | Concurrency | Input Tok/s | Output Tok/s |
|---|---------|---------------|-------------|-------------|--------------|
| 1 | Synchronous | 0.3 | 1.0 | 98 | 46 |
| 2 | Concurrent (4) | 1.3 | 4.0 | 360 | 165 |
| 3 | Concurrent (16) | 4.3 | 15.6 | 1,192 | 545 |
| 4 | Throughput (64) | 12.6 | 62.5 | 3,631 | 1,617 |
| 5 | Constant (5/s) | 4.7 | 17.3 | 1,325 | 604 |
| 6 | Poisson (5/s) | 5.6 | 21.7 | 1,567 | 725 |
| 7 | Sweep (auto) | 0.4 → 15.6 | 1 → 114 | 105 → 4,702 | 49 → 2,002 |

Sweep auto-generated **10 sub-benchmarks** from low to high load.

### 2. Proved EPP Intelligent Routing

Confirmed that the EPP is actively distributing requests across all 4 pods:

**Request distribution (total across all benchmarks):**
| Pod | Role | Total Requests |
|-----|------|---------------|
| klxm5 | Decode | ~351 |
| zmmrt | Decode | ~350 |
| w2dvv | Prefill | ~351 |
| gbxsg | Prefill | ~348 |

**How we proved it:**
- `oc exec` into pods → `curl -sk https://localhost:8000/metrics` for vLLM metrics
- `oc exec` into EPP → `curl -s http://localhost:9090/metrics` for EPP routing metrics
- `oc logs <pod> --since=30s | grep "POST /v1/chat/completions"` for live request counts
- Source IP `10.130.0.37` on all pods = Envoy Gateway (confirmed EPP routing chain)

**EPP routing chain:**
```
GuideLLM → External LB → Envoy Gateway (10.130.0.37) → EPP/Scheduler (10.130.0.35) → Prefill/Decode pods
```

**EPP scoring plugins (configured in EndpointPickerConfig):**
- queue-scorer (weight: 2) -- routes to pod with shortest queue
- kv-cache-utilization-scorer (weight: 2) -- routes to pod with most free KV cache
- prefix-cache-scorer (weight: 3) -- routes to pod that already cached the prompt prefix

### 3. Observability Stack Cleanup

**Deleted old manual Grafana** (from Dec 2025):
- Deployment `grafana`, Service `grafana`, Route `grafana-secure`
- ConfigMaps: `grafana-config`, `grafana-datasources`, `grafana-dashboards-config`, `grafana-dashboard-llm-performance-bd58cmkfmf`
- ServiceAccount `grafana`

**Deleted standalone Prometheus** (was scraping wrong namespace `demo-llm`):
- Deployment `prometheus`, Service `prometheus`, ConfigMap `prometheus-config`
- ServiceAccount `prometheus`, ClusterRole + ClusterRoleBinding `prometheus-llm-d-monitoring`

**What remains in `llm-d-monitoring`:**
- Operator-managed Grafana (`llm-d-grafana`) via Grafana Operator
- GrafanaDatasource pointing to **Thanos Querier** (UWM Prometheus)
- 2 GrafanaDashboards: `vllm-latency-throughput-and-cache` + `epp-routing-and-pool-health`

### 4. Verified Metrics Pipeline

**Full data flow (working):**
```
vLLM pods (port 8000/HTTPS) ──→ UWM Prometheus (PodMonitor) ──→ Thanos Querier ──→ Grafana
EPP pod (port 9090/HTTP)    ──→ UWM Prometheus (ServiceMonitor) ──→ Thanos Querier ──→ Grafana
```

**UWM Prometheus targets (all healthy):**
| Target | Scrape URL | Health |
|--------|-----------|--------|
| Decode 1 (klxm5) | https://10.128.2.21:8000/metrics | up |
| Decode 2 (zmmrt) | https://10.128.2.22:8000/metrics | up |
| Prefill 1 (w2dvv) | https://10.131.0.21:8000/metrics | up |
| Prefill 2 (gbxsg) | https://10.131.0.23:8000/metrics | up |
| EPP Scheduler | http://10.130.0.35:9090/metrics | up |

**Metric name prefix:** PodMonitor relabels `vllm:*` → `kserve_vllm:*`

**Grafana dashboard variables:**
- `$model_name` = `Qwen/Qwen3-0.6B`
- `$namespace` = `my-first-model`

---

## GuideLLM Load Profiles Explained

| Profile | How It Sends Requests | Best For |
|---------|----------------------|----------|
| **Synchronous** | 1 at a time, wait for response | Baseline latency |
| **Concurrent(N)** | Always N in-flight | Controlled parallel load |
| **Throughput** | Flood as fast as possible | Finding max capacity |
| **Constant(N/s)** | Exactly N per second, evenly spaced | Steady-state behavior |
| **Poisson(N/s)** | ~N per second, random intervals (bursty) | Production-like traffic |
| **Sweep** | Auto-ramps from low to high | Finding sweet spot |

Key CLI syntax:
```bash
guidellm benchmark run \
  --target <URL> \
  --model <model-name> \
  --data '{"prompt_tokens":256,"output_tokens":128}' \
  --profile <synchronous|concurrent|throughput|constant|poisson|sweep> \
  --rate <number>          # required for concurrent, throughput, constant, poisson
  --max-seconds 60 \
  --output-dir /results \
  --outputs "name.json,name.html"
```

---

## Key Learnings This Session

### 1. EPP Doesn't Log Per-Request Routing at INFO Level
The EPP scheduler only logs pod discovery and startup events. Per-request routing decisions are NOT logged. Proof of routing comes from:
- Metrics endpoint (`inference_pool_per_pod_queue_size`)
- vLLM pod logs (counting POST requests per pod)
- vLLM metrics (request distribution, KV cache usage)

### 2. LLMInferenceService Controller Reverts Manual Changes
Setting env vars directly on the EPP deployment (`oc set env`) gets reverted by the controller. Changes must go through the `LLMInferenceService` spec.

### 3. Standalone Prometheus Was Scraping Wrong Namespace
The manually deployed Prometheus was configured to scrape `demo-llm` (old namespace). UWM Prometheus with ServiceMonitor/PodMonitor CRs in `my-first-model` was the correct data source all along.

### 4. Bearer Token Types
- OpenShift AI model auth: ServiceAccount JWT token (`eyJhbG...`)
- LiteLLM proxy: Custom API key (`sk-...`)
- Our setup: Auth disabled (`security.opendatahub.io/enable-auth: "false"`)

---

## Files Created/Modified

### Reports (in `./reports/`)
| File | Description |
|------|-------------|
| `sync-benchmarks.html/.json` | Synchronous profile results |
| `concurrent4-benchmarks.html/.json` | Concurrent (4) profile results |
| `concurrent16-benchmarks.html/.json` | Concurrent (16) profile results |
| `throughput-benchmarks.html/.json` | Throughput (64) profile results |
| `constant5-benchmarks.html/.json` | Constant (5 req/s) profile results |
| `poisson5-benchmarks.html/.json` | Poisson (5 req/s) profile results |
| `sweep-benchmarks.html/.json` | Sweep (auto 10 levels) profile results |

### Manifests (in `./manifests/`)
| File | Description |
|------|-------------|
| `05-benchmark-profiles.yaml` | All 7 GuideLLM Job definitions |

### Cluster Resources
| Resource | Namespace | Purpose |
|----------|-----------|---------|
| `pvc-inspector` pod | guidellm-lab | Helper pod to copy results from PVC |
| `guidellm-results-pvc` | guidellm-lab | PVC storing benchmark outputs |
| 7 completed Jobs | guidellm-lab | One per load profile |

---

## Grafana Access

**URL:** https://llm-d-grafana-route-llm-d-monitoring.apps.rosa.openshiftai3.5zpy.p3.openshiftapps.com
**Login:** admin / admin123
**Dashboards:**
1. **llm-d Dashboard** -- vLLM latency, throughput, cache utilization, scheduler state
2. **Inference Gateway** -- EPP routing and pool health

---

## Current State

| Component | Status |
|-----------|--------|
| Prefill pods | 2x Ready |
| Decode pods | 2x Ready |
| EPP Scheduler | Ready |
| All 7 benchmark Jobs | Completed |
| Reports copied to local | Yes (./reports/) |
| Grafana (operator-managed) | Running, connected to UWM Thanos |
| Old manual Grafana | Deleted |
| Standalone Prometheus | Deleted |
| UWM Prometheus | Scraping all 5 targets (healthy) |
