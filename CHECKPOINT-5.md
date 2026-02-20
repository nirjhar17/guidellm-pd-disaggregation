# Checkpoint 5 — EPP Fixed, Prefix Caching Working, Session Affinity Active

**Date:** 2026-02-20

## Summary

Today we discovered the EPP (Endpoint Picker Pod) was never actually routing requests. What we thought was "intelligent routing" in previous checkpoints was just Envoy's default round-robin. We fixed three separate issues and then enabled prefix caching with session affinity, achieving 86% cache hit rates.

## The Problem

Previous benchmarks showed equal distribution (25/25/25/25) across all 4 pods. We assumed this was the EPP's queue-scorer balancing load evenly. It was not. The EPP was dead.

## Root Cause Chain

### Issue 1: EnvoyFilter pointing to "dummy" cluster

The Envoy Gateway had a base `ext_proc` HTTP filter auto-configured with:
- `cluster_name: "dummy"`
- `request_header_mode: SKIP`

This meant Envoy never sent any request to the EPP. The per-route `EnvoyFilter` (targeting `HTTP_ROUTE`) could not override the base filter's SKIP mode.

**Fix:** Changed the EnvoyFilter to target `applyTo: HTTP_FILTER` (base filter level) instead of `HTTP_ROUTE`, replacing `dummy` with the real EPP cluster:
```
cluster_name: outbound|9002||qwen3-0-6b-epp-service.my-first-model.svc.cluster.local
```
**Evidence:** Envoy metrics changed from `cx_total: 0` to `cx_total: 5` after applying.

### Issue 2: TLS handshake failure (CERTIFICATE_VERIFY_FAILED)

After Fix 1, Envoy attempted to connect but every connection failed (`cx_connect_fail: 5/5`). The EPP was presenting a self-signed certificate because its `--cert-path` argument was missing, even though the service-CA signed cert was mounted at `/var/run/kserve/tls`.

**Fix:** Added `--cert-path=/var/run/kserve/tls` and `--secure-serving` to the EPP scheduler args via LLMInferenceService patch.
**Evidence:** Envoy metrics changed to `cx_connect_fail: 0`, requests returned HTTP 200.

### Issue 3: Wrong EndpointPickerConfig (no P/D awareness)

The default config used `single-profile-handler` with a single `default` profile containing `queue-scorer`, `kv-cache-utilization-scorer`, `prefix-cache-scorer`. This treats all pods the same — no distinction between prefill and decode.

**Fix:** Replaced with P/D-aware config:
- `pd-profile-handler` with separate `prefill` and `decode` profiles
- `prefill-filter` and `decode-filter` to route by pod role
- `queue-scorer` for load balancing within each group
- `max-score-picker` for endpoint selection

**Evidence:** Distribution changed from 25/25/25/25 to prefill 60/40, decode 53/47 (100 requests). Every request went through prefill first, then decode.

### Issue 4: Prefix caching not enabled in vLLM

vLLM was running with `VLLM_USE_V1=0` (v0 engine where APC is off by default) and no `--enable-prefix-caching` flag. The `prefix_cache_queries_total` metric was non-zero but `prefix_cache_hits_total` was always 0.

**Fix:** Added `--enable-prefix-caching --block-size=16` to `VLLM_ADDITIONAL_ARGS` for both decode and prefill pods. Also added `prefix-cache-scorer` (weight 3) to both scheduling profiles in the EPP config.

**Evidence:** Cache hit rate jumped to 86% with 100 shared-prefix requests. Session affinity kicked in — one decode pod received 100/100 requests (warm cache), the other received 0.

## Final Results

### 100 curl requests (same shared prompt):
| Pod | Role | Requests | Cache Queries | Cache Hits | Hit Rate |
|-----|------|----------|---------------|------------|----------|
| ckhgt | decode | 0 | 0 | 0 | N/A |
| l95rd | decode | 100 | 5,500 | 4,752 | 86.4% |
| 9fnp4 | prefill | 46 | 2,530 | 2,160 | 85.4% |
| f2cqp | prefill | 54 | 2,970 | 2,544 | 85.7% |

### inference-perf shared prefix run (270 requests, 3 stages):
| Pod | Role | Requests | Cache Hits | Hit Rate |
|-----|------|----------|------------|----------|
| ckhgt | decode | +219 | +36,496 | 82.8% |
| l95rd | decode | +51 | +8,384 | 81.7% |
| 9fnp4 | prefill | +144 | +21,952 | 75.8% |
| f2cqp | prefill | +126 | +18,816 | 74.2% |

### inference-perf performance metrics:
- **270 requests, 0 failures**
- TTFT median: 94.5ms
- TPOT median: 23.4ms
- Request latency median: 1.62s
- Throughput: 2.72 req/s, 712 tokens/s

## Comparison Across All Phases

| Phase | Distribution | Cache Hits | EPP Status |
|-------|-------------|------------|------------|
| Before all fixes | 25/25/25/25 | 0% | Dead (dummy cluster) |
| After EnvoyFilter + TLS + P/D fix | prefill 60/40, decode 53/47 | 0% | Working (P/D routing) |
| After prefix-cache + session affinity | prefill 46/54, decode 100/0 | ~86% | Full (P/D + cache-aware + sticky) |

## Current LLMInferenceService Config

### EPP Scheduler Args:
```
--pool-group inference.networking.x-k8s.io
--secure-serving
--model-server-metrics-scheme https
--model-server-metrics-https-insecure-skip-verify
--cert-path /var/run/kserve/tls
--config-text <P/D EndpointPickerConfig>
```

### EndpointPickerConfig:
```yaml
plugins:
- type: prefill-header-handler
- type: prefill-filter
- type: decode-filter
- type: max-score-picker
- type: queue-scorer
- type: prefix-cache-scorer
- type: pd-profile-handler
  parameters:
    threshold: 0
schedulingProfiles:
- name: prefill
  plugins:
  - pluginRef: prefill-filter
  - pluginRef: queue-scorer
    weight: 1.0
  - pluginRef: prefix-cache-scorer
    weight: 3.0
  - pluginRef: max-score-picker
- name: decode
  plugins:
  - pluginRef: decode-filter
  - pluginRef: queue-scorer
    weight: 1.0
  - pluginRef: prefix-cache-scorer
    weight: 3.0
  - pluginRef: max-score-picker
```

### vLLM ADDITIONAL_ARGS:
```
--dtype=half --max-model-len=2048 --max-num-seqs=64 --gpu-memory-utilization=0.40
--enforce-eager --enable-auto-tool-choice --tool-call-parser hermes
--enable-prefix-caching --block-size=16
```

### EnvoyFilter (openshift-ingress namespace):
```yaml
applyTo: HTTP_FILTER
match:
  context: GATEWAY
  listener:
    filterChain:
      filter:
        name: envoy.filters.network.http_connection_manager
        subFilter:
          name: envoy.filters.http.ext_proc
patch:
  operation: MERGE
  value:
    typed_config:
      '@type': type.googleapis.com/envoy.extensions.filters.http.ext_proc.v3.ExternalProcessor
      grpc_service:
        envoy_grpc:
          cluster_name: outbound|9002||qwen3-0-6b-epp-service.my-first-model.svc.cluster.local
        timeout: 30s
      failure_mode_allow: true
      processing_mode:
        request_header_mode: SEND
        response_header_mode: SEND
        request_body_mode: STREAMED
      message_timeout: 30s
```

## Slack Question

The original Slack question about EPP not routing unequally is now answered. The EPP was genuinely broken (dummy cluster + TLS failure + wrong config). After fixing all three issues and enabling prefix caching, we achieved:
- True P/D disaggregated routing
- 86% prefix cache hit rate (close to the Red Hat blog's 87.4%)
- Session affinity (100% decode requests to warmest pod)

The Slack question can be deleted — mission accomplished.
