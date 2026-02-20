---
layout: default
title: Benchmarking Prefill/Decode Disaggregation on OpenShift AI with GuideLLM
---

# Benchmarking Prefill/Decode Disaggregation on OpenShift AI with GuideLLM

We have deployed our LLM on OpenShift AI. The inference pods are running. We can curl it. But how do we know if the setup is actually performing well? How do we find the sweet spot between latency and throughput? And with Prefill/Decode disaggregation, where prompt processing and token generation run on separate pods, is the intelligent router actually doing its job?

Without llm-d, a standard LLM deployment runs prefill and decode together on the same pod. Every request competes for the same GPU, the same memory, and the same compute. Prefill (processing the full prompt) is compute-heavy, while decode (generating tokens one at a time) is memory-bound. Bundling them together means neither phase runs at its best, and scaling means duplicating everything.

llm-d changes this by disaggregating prefill and decode into separate pods that can scale independently. Prefill pods can be optimized for throughput, decode pods for latency, and each can be scaled based on actual demand. This is what makes LLM inference truly scalable on Kubernetes.

The intelligent routing comes from the EPP, the Endpoint Picker Pod. The EPP sits between the gateway and the model-serving pods, scoring every available pod on queue depth, KV cache utilization, and prefix cache hits before routing each request to the best candidate. This is not round-robin load balancing. This is inference-aware scheduling that understands the internal state of each vLLM instance.

We have this setup running on our ROSA cluster, and now the question is: how does it actually perform under pressure? This blog walks through our benchmarking session where we used GuideLLM to run 7 different load profiles, each simulating a different type of traffic pattern, to understand how latency and throughput behave as we increase the load.

> **Update (Feb 2026):** During our investigation, we discovered the EPP was not actually routing requests intelligently during the initial GuideLLM benchmarks. Four separate issues prevented EPP from functioning: a dummy ext_proc cluster in Envoy, a TLS handshake failure, an incorrect EndpointPickerConfig, and missing prefix caching flags on vLLM. The throughput gains in the GuideLLM sweep came from having 4 pods across 2 GPUs, not from intelligent scoring. After fixing all four issues, we re-ran inference-perf and achieved 86% prefix cache hit rates with true session affinity. The full debugging journey and fixes are documented in [our EPP troubleshooting guide](https://github.com/nirjhar17/llm-d-observability-openshift/blob/main/llm-d-request-flow-guide.md#4-the-bug).

## Our Setup

We're running on ROSA HCP 4.20.6 (AWS ap-southeast-1) with OpenShift AI. The model is Qwen/Qwen3-0.6B served by vLLM, with llm-d managing the inference stack. We have 2 GPU nodes, each with an NVIDIA Tesla T4 (16 GiB VRAM).

The deployment uses Prefill/Decode disaggregation. Here's the request flow:

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

The prefill pods receive the full prompt and process all tokens in parallel, building the KV cache. That cache is transferred to the decode pods, which generate output tokens one at a time. The EPP (Endpoint Picker) router scores every pod on queue depth and prefix cache hits, then picks the best one for each request.

## How a Request Travels Through the Stack

Before diving into the config, it helps to understand what happens when you send a chat completion request. There are multiple components involved, each in a different namespace, and the request passes through all of them:

```
YOU                     GATEWAY POD               EPP POD             vLLM POD
 |                          |                        |                    |
 |---POST /v1/chat/----->   |                        |                    |
 |   completions            |                        |                    |
 |   (with JSON body)       |                        |                    |
 |                          |                        |                    |
 |                     1. Receives your request      |                    |
 |                     2. Matches the URL to an      |                    |
 |                        HTTPRoute rule             |                    |
 |                     3. Rule says: "this goes      |                    |
 |                        to InferencePool"          |                    |
 |                     4. Sends headers+body ------> |                    |
 |                        to EPP via ext-proc        |                    |
 |                                                   |                    |
 |                                              5. EPP scores            |
 |                                                 all vLLM pods         |
 |                                              6. Picks the best one    |
 |                                              7. Returns "use pod      |
 |                     8. Receives EPP's  <------   10.128.16.25"        |
 |                        decision                   |                    |
 |                     9. Forwards the FULL          |                    |
 |                        request (headers+body) -----------------------> |
 |                                                                   10. vLLM runs
 |                                                                       the model
 |                                                                   11. Returns
 |                     12. Forwards response <------------------------   tokens
 | <---response------- back to you                                       |
 |                          |                        |                    |
```

The **Gateway pod** (in `openshift-ingress` namespace) runs an Envoy proxy controlled by Istio's Service Mesh. When your request arrives, Envoy matches the URL path against an **HTTPRoute** (in `my-first-model` namespace) which points to an **InferencePool**. The InferencePool tells Envoy: "don't just round-robin this — send it to the EPP first for a routing decision."

Envoy talks to the EPP using a mechanism called **ext_proc** (External Processing). This is a gRPC call where Envoy sends the request headers and body to the EPP, and the EPP responds with "route this to pod X." The ext_proc configuration lives inside the Envoy proxy's running config, and on RHOAI 3.0–3.2, this config needs manual fixing via an EnvoyFilter (explained below).

The **EPP pod** (in `my-first-model` namespace) receives the ext_proc call and scores all available vLLM pods using its configured plugins. For P/D disaggregation, it first determines whether this is a prefill or decode phase, filters to only the relevant pods, then scores them on queue depth and prefix cache hits. It returns the IP of the best pod back to Envoy.

The **vLLM pod** (in `my-first-model` namespace) receives the actual HTTP request from Envoy and runs inference. The vLLM instance has no idea about the EPP — from its perspective, it just received a normal request from the gateway.

Only inference requests (`/v1/chat/completions` and `/v1/completions`) go through the EPP for intelligent routing. Other paths like `/v1/models` bypass the EPP entirely and go directly to any vLLM pod via round-robin.

## The LLMInferenceService with P/D Disaggregation

The core of our deployment is a single LLMInferenceService CR that defines both the prefill and decode pods, along with the EPP scheduler. Here is the corrected YAML that produces a fully working P/D disaggregation setup with prefix caching and intelligent routing:

```yaml
apiVersion: serving.kserve.io/v1alpha1
kind: LLMInferenceService
metadata:
  name: qwen3-0-6b
  namespace: my-first-model
spec:
  model:
    name: Qwen/Qwen3-0.6B
    uri: hf://Qwen/Qwen3-0.6B

  # Decode pods
  replicas: 2
  template:
    containers:
    - name: main
      image: vllm/vllm-openai:v0.11.2
      env:
      - name: VLLM_ADDITIONAL_ARGS
        value: >-
          --dtype=half
          --max-model-len=2048
          --max-num-seqs=64
          --gpu-memory-utilization=0.40
          --enforce-eager
          --enable-prefix-caching
          --block-size=16
      resources:
        requests:
          nvidia.com/gpu: "1"
        limits:
          nvidia.com/gpu: "1"

  # Prefill pods
  prefill:
    replicas: 2
    template:
      containers:
      - name: main
        image: vllm/vllm-openai:v0.11.2
        env:
        - name: VLLM_ADDITIONAL_ARGS
          value: >-
            --dtype=half
            --max-model-len=2048
            --max-num-seqs=64
            --gpu-memory-utilization=0.40
            --enforce-eager
            --enable-prefix-caching
            --block-size=16

  # EPP Scheduler with P/D-aware routing and prefix cache scoring
  router:
    gateway: {}
    route: {}
    scheduler:
      template:
        containers:
        - name: main
          args:
          - --pool-group
          - inference.networking.x-k8s.io
          - --pool-name
          - "{{ ChildName .ObjectMeta.Name `-inference-pool` }}"
          - --pool-namespace
          - "{{ .ObjectMeta.Namespace }}"
          - --zap-encoder
          - json
          - --grpc-port
          - "9002"
          - --grpc-health-port
          - "9003"
          - --secure-serving
          - --model-server-metrics-scheme
          - https
          - --model-server-metrics-https-insecure-skip-verify
          - --cert-path
          - /var/run/kserve/tls
          - --config-text
          - |
            apiVersion: inference.networking.x-k8s.io/v1alpha1
            kind: EndpointPickerConfig
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

There are several critical differences from a default LLMInferenceService:

**vLLM args** include `--enable-prefix-caching --block-size=16` on both decode and prefill pods. Without these, vLLM's Automatic Prefix Caching is disabled (it is off by default in the v0 engine), and the EPP's prefix-cache-scorer has nothing to score against.

**EPP scheduler args** include `--secure-serving` and `--cert-path=/var/run/kserve/tls`. The TLS certificate is already mounted by KServe at that path, but the EPP does not use it unless told to. Without these args, the EPP presents a self-signed certificate, and Envoy rejects the TLS handshake.

**EndpointPickerConfig** uses `pd-profile-handler` with separate `prefill` and `decode` scheduling profiles instead of a single `default` profile. The `prefill-filter` and `decode-filter` plugins ensure that prefill requests only go to prefill pods and decode requests only go to decode pods. The `prefix-cache-scorer` with weight 3.0 in both profiles gives strong preference to pods that already have the matching prompt prefix cached, creating session affinity for shared-prefix workloads.

The `spec.replicas` controls decode pod count, `spec.prefill.replicas` controls prefill. One YAML creates the entire disaggregated inference stack — vLLM pods, EPP scheduler, InferencePool, HTTPRoute, Services, and DestinationRules.

The full manifest with all fields (nodeSelector, tolerations, resource limits) is available at [manifests/01-llminferenceservice-pd.yaml](https://github.com/nirjhar17/guidellm-pd-disaggregation/blob/main/manifests/01-llminferenceservice-pd.yaml).

## The EnvoyFilter for EPP Routing

The LLMInferenceService creates everything needed for intelligent routing — except one thing. On RHOAI 3.0–3.2 (which uses Istio 1.26.x), the Envoy Gateway's ext_proc filter is auto-configured with a placeholder that disables it.

We discovered this by dumping the live Envoy configuration from inside the Gateway pod:

```bash
ENVOY_POD=$(oc get pods -n openshift-ingress \
  -l gateway.networking.k8s.io/gateway-name=openshift-ai-inference \
  -o jsonpath='{.items[0].metadata.name}')

oc exec -n openshift-ingress $ENVOY_POD -c istio-proxy -- \
  pilot-agent request GET config_dump | python3 -c "
import json, sys
data = json.load(sys.stdin)
for config in data.get('configs', []):
    for resource in config.get('dynamic_listeners', []):
        print(json.dumps(resource, indent=2))
" | grep -A5 ext_proc
```

Deep inside the output, the base ext_proc HTTP filter had:

```
cluster_name: "dummy"
request_header_mode: SKIP
```

This means Envoy never sends requests to the EPP. Every request bypasses intelligent routing and goes through Envoy's default round-robin. The EPP pod is running and healthy, but nobody is talking to it.

To fix this, we apply an **EnvoyFilter** that patches the base ext_proc filter inside the Gateway's Envoy proxy, replacing the dummy cluster with the real EPP service:

```yaml
apiVersion: networking.istio.io/v1alpha3
kind: EnvoyFilter
metadata:
  name: fix-extproc-body-mode
  namespace: openshift-ingress
spec:
  workloadSelector:
    labels:
      gateway.networking.k8s.io/gateway-name: openshift-ai-inference
  configPatches:
  - applyTo: HTTP_FILTER
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

Here is what this YAML does line by line:

- `workloadSelector` targets only the Envoy pods belonging to the `openshift-ai-inference` Gateway, not every Envoy in the mesh.
- `applyTo: HTTP_FILTER` means we are patching the **base** ext_proc filter, not a per-route override. This is critical — per-route overrides (`HTTP_ROUTE`) cannot un-SKIP a base filter that is set to SKIP.
- `operation: MERGE` means we are merging our config into the existing base filter. There is already a base ext_proc filter registered by Istio (the one with `dummy`/`SKIP`). We are overwriting the broken fields while keeping the rest.
- `cluster_name` points to the actual EPP gRPC service: `outbound|9002||qwen3-0-6b-epp-service.my-first-model.svc.cluster.local`. This is Envoy's internal cluster name format — it resolves to the EPP service on port 9002.
- `request_header_mode: SEND` and `response_header_mode: SEND` replace the `SKIP` default, telling Envoy to actually send request and response headers to the EPP for processing.
- `request_body_mode: STREAMED` tells Envoy to stream the request body to the EPP. This replaced the broken `FULL_DUPLEX_STREAMED` mode in Istio 1.26.2 that was causing request bodies to disappear.
- `failure_mode_allow: true` means if the EPP is down, requests still go through (round-robin fallback) instead of returning 500 errors.
- `message_timeout: 30s` gives the EPP up to 30 seconds to respond before Envoy times out. The default is too short for large prompts.

**Why is this manual?** This is a gap in RHOAI 3.0–3.2. When KServe creates the InferencePool and HTTPRoute, Istio registers the ext_proc filter but configures it with a placeholder dummy cluster. No operator currently patches this automatically. When RHOAI upgrades to a version where this wiring is automated, this EnvoyFilter can be removed.

To verify the EnvoyFilter is working, check the Envoy proxy metrics:

```bash
ENVOY_POD=$(oc get pods -n openshift-ingress \
  -l gateway.networking.k8s.io/gateway-name=openshift-ai-inference \
  -o jsonpath='{.items[0].metadata.name}')

oc exec -n openshift-ingress $ENVOY_POD -c istio-proxy -- \
  pilot-agent request GET /clusters | grep "epp-service"
```

`cx_total` should be greater than 0 and `cx_connect_fail` should be 0. If `cx_total` is 0, the EnvoyFilter is not applied or is targeting the wrong filter level.

## GPU Time-Slicing for Multi-Pod Per GPU

With only 2 physical T4 GPUs and 4 model-serving pods needed, we had to configure GPU time-slicing. This exposes each physical GPU as 4 virtual GPU slots in Kubernetes.

The first step was creating a ConfigMap in the NVIDIA GPU Operator namespace:

```
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

Then we patched the ClusterPolicy to reference this ConfigMap. The ClusterPolicy is a cluster-scoped CR managed by the NVIDIA GPU Operator. It controls how the device plugin, driver, and toolkit behave across all GPU nodes. We need to tell it where our time-slicing config lives:

```
oc patch clusterpolicy gpu-cluster-policy --type=merge -p '{
  "spec": {
    "devicePlugin": {
      "config": {
        "name": "device-plugin-config",
        "default": ""
      }
    }
  }
}'
```

This patch does two things. It tells the GPU Operator to read time-slicing settings from our ConfigMap called device-plugin-config. It also sets the default to empty, which means a GPU node only gets time-slicing if we explicitly label it. Without the label, the node keeps its original single-GPU behavior. This gives us per-node control over which GPUs are shared.

After applying the patch, the GPU Operator restarts the device plugin DaemonSet pods. Once they come back, any node with the matching label will advertise 4 nvidia.com/gpu instead of 1.

Finally, each GPU node needs the label nvidia.com/device-plugin.config=Tesla-T4 to activate time-slicing. We set this in the ROSA machinepool definition so every new node gets it automatically:

```
rosa edit machinepool gpu \
  --cluster=<cluster-id> \
  --labels="nvidia.com/device-plugin.config=Tesla-T4"
```

There is one critical thing to understand about GPU time-slicing. It does not split VRAM into isolated partitions. Every pod sharing a GPU can see and allocate from the full 16 GiB. There is no memory fence between them. This means if two pods each try to use 85% of VRAM, they would attempt to allocate 13.6 GiB plus 13.6 GiB, which is 27.2 GiB on a 16 GiB card. That causes an out-of-memory crash.

To prevent this, we control how much VRAM each pod is allowed to use through vLLM's gpu-memory-utilization setting. We set it to 0.40 (40%) per pod inside the VLLM_ADDITIONAL_ARGS in the LLMInferenceService YAML we showed earlier:

```
env:
- name: VLLM_ADDITIONAL_ARGS
  value: >-
    --dtype=half
    --max-model-len=2048
    --max-num-seqs=64
    --gpu-memory-utilization=0.40
    --enforce-eager
```

With two pods on one GPU at 40% each, they use 6.4 GiB plus 6.4 GiB, which is 12.8 GiB total. That leaves 3.2 GiB of headroom on the 16 GiB T4.

The other two parameters in that block also affect memory. max-model-len controls the maximum sequence length, and max-num-seqs controls how many requests can run concurrently. Both directly determine how large the KV cache grows in GPU memory. We tuned them down from 4096/256 to 2048/64 so the KV cache fits comfortably within each pod's 40% VRAM budget.

All infrastructure manifests are in our [repository](https://github.com/nirjhar17/guidellm-pd-disaggregation/tree/main/manifests).

## Why GuideLLM

GuideLLM is a benchmarking tool built by the vLLM team specifically for LLM inference workloads. It speaks the OpenAI API natively, supports streaming, and offers load profiles that map to real-world scenarios, not just "blast as many requests as possible."

We ran GuideLLM as Kubernetes Jobs inside the cluster, targeting the Envoy Gateway URL, not the vLLM pods directly. This ensures every request flows through the full path: Gateway to EPP to vLLM.

```
apiVersion: batch/v1
kind: Job
metadata:
  name: guidellm-profile-synchronous
  namespace: guidellm-lab
spec:
  template:
    spec:
      containers:
      - name: guidellm
        image: ghcr.io/vllm-project/guidellm:v0.5.0
        env:
        - name: HOME
          value: /results
        command: ["guidellm"]
        args:
        - "benchmark"
        - "run"
        - "--target"
        - "http://<gateway-url>/my-first-model/qwen3-0-6b"
        - "--model"
        - "Qwen/Qwen3-0.6B"
        - "--data"
        - '{"prompt_tokens":256,"output_tokens":128}'
        - "--profile"
        - "synchronous"
        - "--max-seconds"
        - "60"
        - "--output-dir"
        - "/results"
        - "--outputs"
        - "sync-benchmarks.json,sync-benchmarks.html"
        volumeMounts:
        - name: results
          mountPath: /results
      volumes:
      - name: results
        persistentVolumeClaim:
          claimName: guidellm-results-pvc
      restartPolicy: Never
  backoffLimit: 1
```

Each job writes results to a shared PVC. We used a pvc-inspector helper pod to copy files out after all jobs completed.

## The 7 Load Profiles

We ran each profile for 60 seconds with prompt_tokens=256 and output_tokens=128. Here's what each does and why it matters.

Synchronous sends one request at a time. It waits for the full response before sending the next. This gives you the best-case latency when the system has zero contention. Use --profile synchronous.

Concurrent keeps exactly N requests in flight at all times. When one completes, the next is sent immediately. We tested N=4 and N=16. Use --profile concurrent --rate 4.

Throughput fires requests as fast as possible with no rate limiting. This finds the system's ceiling, the maximum requests per second before everything degrades. Use --profile throughput --rate 64.

Constant sends exactly N requests per second, evenly spaced. This simulates a predictable, steady workload. Use --profile constant --rate 5.

Poisson sends roughly N requests per second, but with random intervals following a Poisson distribution. This creates the bursty patterns you see in real production traffic. Use --profile poisson --rate 5.

Sweep automatically runs a synchronous baseline, a throughput ceiling test, then 8 constant-rate tests at evenly spaced rates between the two extremes. This gives you a complete latency-vs-throughput curve in a single run. Use --profile sweep.

## The Results

Here are the results across all profiles. TTFT is Time To First Token, how long until the model starts responding. ITL is Inter-Token Latency, how fast tokens stream after the first one.

| Profile | --profile | --rate | Requests | RPS | TTFT Median | ITL Median | Latency Median |
|---|---|---|---|---|---|---|---|
| Synchronous | synchronous | - | 22 | 0.35 | 96.6ms | 20.4ms | 2.81s |
| Concurrent (4) | concurrent | 4 | 78 | 1.28 | 97.6ms | 23.3ms | 2.90s |
| Concurrent (16) | concurrent | 16 | 278 | 4.62 | 108.6ms | 26.1ms | 3.17s |
| Throughput (64) | throughput | 64 | 738 | 12.28 | 183.3ms | 39.0ms | 4.61s |
| Constant (5/s) | constant | 5 | 282 | 4.70 | 102.5ms | 26.6ms | 3.17s |
| Poisson (5/s) | poisson | 5 | 337 | 5.60 | 124.1ms | 29.3ms | 3.83s |
| Sweep (auto) | sweep | - | 22–902 | 0.35–14.93 | 92.4–161.9ms | 21.7–47.5ms | 2.79–16.14s |

The baseline TTFT with EPP active is 92-97ms at low load, compared to 54-64ms in our earlier run when EPP was not active. The difference is the EPP scoring overhead — every request now goes through the ext_proc call where the EPP evaluates all pods on queue depth and prefix cache hits before returning a routing decision. This adds ~30-40ms per request at idle.

The sweet spot is 5-8 RPS, where TTFT stays under 130ms and the system feels responsive. The ceiling is 12.28 RPS in throughput mode. Beyond that, TTFT and ITL both climb sharply.

For capacity planning, use the Poisson results, not Constant. At the same 5 RPS target, Poisson showed 21% worse TTFT (124.1ms vs 102.5ms) because real traffic arrives in bursts that create momentary queue spikes. If this model serves real users, plan for 10-11 RPS per set of 4 pods with headroom for bursts.

## Verifying EPP Routing

After applying the LLMInferenceService and EnvoyFilter, the next step is verifying that the EPP is actually in the loop and routing requests intelligently. There are three commands that tell you everything you need to know.

**1. Check Envoy-to-EPP connectivity:**

```bash
ENVOY_POD=$(oc get pods -n openshift-ingress \
  -l gateway.networking.k8s.io/gateway-name=openshift-ai-inference \
  -o jsonpath='{.items[0].metadata.name}')

oc exec -n openshift-ingress $ENVOY_POD -c istio-proxy -- \
  pilot-agent request GET /clusters | grep "epp-service"
```

Look for `cx_total` (total connections attempted) and `cx_connect_fail` (failed connections). If `cx_total` is 0, the EnvoyFilter is not applied and Envoy is still using the dummy cluster. If `cx_connect_fail` equals `cx_total`, the TLS handshake is failing (missing `--cert-path` on the EPP).

**2. Check per-pod request distribution:**

```bash
for pod in $(oc get pods -n my-first-model \
  -l serving.kserve.io/inferenceservice=qwen3-0-6b \
  -o name); do
  echo "=== $pod ==="
  oc exec -n my-first-model $pod -- \
    curl -s localhost:8000/metrics | grep "vllm:request_success_total"
done
```

With EPP working and shared-prefix workloads, you should see unequal distribution. The EPP's `prefix-cache-scorer` creates session affinity — requests with the same prefix get routed to the pod that already has the cached KV entries. In our tests with 100 shared-prefix requests, one decode pod received all 100 requests while the other received 0.

**3. Check prefix cache hit rate:**

```bash
for pod in $(oc get pods -n my-first-model \
  -l serving.kserve.io/inferenceservice=qwen3-0-6b \
  -o name); do
  echo "=== $pod ==="
  oc exec -n my-first-model $pod -- \
    curl -s localhost:8000/metrics | grep "prefix_cache"
done
```

After sending shared-prefix requests, `vllm:prefix_cache_hits_total` should be non-zero. We measured 86% hit rate with the EPP active, compared to 65% with round-robin (where each pod builds its own partial cache independently).

## GuideLLM Output Formats

GuideLLM generates four output formats: HTML for visual charts, JSON for the full authoritative record with all percentiles and per-request timings, YAML for human-readable inspection, and CSV for spreadsheets. Use --outputs to request any combination, for example: --outputs "results.json,results.html,results.csv"

## Comparing With and Without P/D Disaggregation

Before enabling P/D disaggregation, we ran the same GuideLLM sweep benchmark against a standard KServe deployment of the same Qwen3-0.6B model on the same hardware. That benchmark targeted the vLLM workload service directly with no EPP routing and no prefill/decode split. The full walkthrough of that benchmark is in our earlier blog: [Exploring GuideLLM: Benchmarking a Live LLM on OpenShift](https://medium.com/@jajodia.nirjhar/exploring-guidellm-benchmarking-a-live-llm-on-openshift-ccc2d0841794).

Here is what changed when we enabled P/D disaggregation with the EPP fully operational.

Maximum throughput went from 8.53 RPS (single pod) to 14.93 RPS (sweep ceiling with P/D + EPP). That is a 1.75x improvement. The standard deployment hit its ceiling with a single vLLM pod handling both prefill and decode on one GPU. With P/D, the work is split across 4 pods (2 prefill + 2 decode) on 2 GPUs, and the EPP routes each request to the pod with the shortest queue and warmest prefix cache.

The saturation point shifted from 5-6 RPS to 13-14 RPS. In the standard deployment, latency exploded around 5-6 RPS. With P/D disaggregation and EPP routing, TTFT stays under 165ms all the way to 13.4 RPS, then collapses to 18,000ms at the throughput ceiling (14.9 RPS). The "green zone" where latency stays flat and predictable is nearly 3x wider.

Baseline TTFT is higher with P/D + EPP: 93-97ms vs 32ms at low load. The overhead comes from two things: the extra network hop through the Envoy Gateway (~30ms), plus the EPP scoring call where it evaluates all pods via ext_proc (~30-40ms). This trade-off pays for itself under load because the standard deployment was already at degraded TTFT by the time it hit 6 RPS, while the P/D setup maintains stable TTFT up to 13 RPS.

Inter-Token Latency was nearly identical in both setups: 19.74ms without P/D vs 21.2ms with P/D at low load. This makes sense because ITL is determined by the vLLM engine and GPU speed during the decode phase, not the routing layer. The EPP only routes the initial request. Once token generation starts, it streams directly from the decode pod to the client.

Request latency at low load: 2.54s without P/D vs 2.81s with P/D. The 270ms difference comes from the routing overhead (Gateway + EPP ext_proc). At high load, this gap reverses because the P/D setup handles queuing and contention much better with 4 pods instead of 1.

One important caveat with GuideLLM results: GuideLLM generates random synthetic prompts for every request. No two prompts share a prefix. This means the prefix-cache-scorer (our highest weighted plugin at 3.0) has nothing to score against. The EPP falls back to queue-scorer for routing decisions. To exercise the prefix cache, we used inference-perf with shared-prefix workloads (see below).

## Proving Prefix Cache with Shared Prompts

The GuideLLM comparison above showed throughput and latency gains, but it did not exercise the prefix cache. In production, real workloads like chatbots and RAG systems send many requests that share the same system prompt. To prove the prefix cache actually works, we needed a different tool.

We used [inference-perf](https://github.com/kubernetes-sigs/inference-perf), a benchmarking tool from the Kubernetes SIG Serving project that is also used by the [llm-d-benchmark](https://github.com/llm-d/llm-d-benchmark) framework. Unlike GuideLLM, inference-perf has a built-in shared_prefix data generator that creates requests where multiple prompts share the same system prompt prefix with only the user question varying.

Here is the config we used:

```
load:
  type: constant
  stages:
  - rate: 1
    duration: 30
  - rate: 3
    duration: 30
  - rate: 5
    duration: 30
api:
  type: completion
  streaming: true
server:
  type: vllm
  model_name: Qwen/Qwen3-0.6B
  base_url: http://<gateway-url>/my-first-model/qwen3-0-6b
  ignore_eos: true
tokenizer:
  pretrained_model_name_or_path: Qwen/Qwen3-0.6B
data:
  type: shared_prefix
  shared_prefix:
    num_unique_system_prompts: 5
    num_users_per_system_prompt: 20
    system_prompt_len: 128
    question_len: 64
    output_len: 64
```

This generates 5 distinct system prompts, each 128 tokens long, with 20 unique user questions per prompt. The target URL goes through the Gateway and EPP, not directly to vLLM.

We saved this config as a file and created a ConfigMap from it:

```
oc create configmap inference-perf-config \
  --from-file=06-inference-perf-shared-prefix-config.yml \
  -n guidellm-lab
```

Then we ran it as a Kubernetes Job that mounts the ConfigMap and passes the config file to the inference-perf CLI:

```
apiVersion: batch/v1
kind: Job
metadata:
  name: inference-perf-shared-prefix
  namespace: guidellm-lab
spec:
  template:
    spec:
      containers:
      - name: inference-perf
        image: quay.io/inference-perf/inference-perf:latest
        command: ["inference-perf"]
        args:
        - "--config_file"
        - "/etc/config/06-inference-perf-shared-prefix-config.yml"
        env:
        - name: HOME
          value: /tmp
        - name: HF_HOME
          value: /tmp/hf_home
        volumeMounts:
        - name: config-volume
          mountPath: /etc/config
          readOnly: true
      restartPolicy: Never
      volumes:
      - name: config-volume
        configMap:
          name: inference-perf-config
```

The HOME and HF_HOME env vars are needed because the container's default home directory is not writable, and the tokenizer download requires a cache directory.

Before starting the job, we recorded the prefix cache metrics on all 4 pods. They were all at zero (fresh pods, no prior requests).

**Initial run (before EPP was fixed):** The first time we ran inference-perf, the EPP was not active due to the four bugs described in "Fixing EPP Intelligent Routing" below. Requests went through Envoy round-robin. All 4 pods received roughly equal traffic and prefix cache hits were ~65%. This was vLLM's local Automatic Prefix Caching (APC) working within each individual pod, not cross-pod routing by the EPP.

**After fixing the EPP:** We re-ran inference-perf with 100 shared-prefix requests. This time, with EPP fully operational and the `prefix-cache-scorer` active (weight 3), the results were dramatically different:

- Prefix cache hit rate: **86%** (up from 65% with round-robin)
- Decode pod distribution: **100 requests on 1 pod, 0 on the other** (session affinity — the EPP routes requests to the pod that already has the matching prefix cached)
- Prefill pod distribution: 46 + 54 (split across both, as expected for prefill)

The 86% hit rate is close to the theoretical maximum reported in the [Red Hat blog on KV-cache-aware routing](https://developers.redhat.com/articles/2025/10/07/master-kv-cache-aware-routing-llm-d-efficient-ai-inference) (87.4%). The session affinity on decode pods is the key: instead of spreading requests across all pods (where each pod builds its own partial cache), the EPP concentrates shared-prefix requests on the pod that already has the warm cache.

KV cache usage stayed under 3% throughout the run. Because cached prefixes are reused rather than reallocated, memory consumption stays flat even as more requests arrive. This is the efficiency gain that makes prefix caching valuable at scale.

The inference-perf config and Job manifest are in our repository at [manifests/06-inference-perf-shared-prefix-config.yml](https://github.com/nirjhar17/guidellm-pd-disaggregation/blob/main/manifests/06-inference-perf-shared-prefix-config.yml) and [manifests/07-inference-perf-job.yaml](https://github.com/nirjhar17/guidellm-pd-disaggregation/blob/main/manifests/07-inference-perf-job.yaml).

## Fixing EPP Intelligent Routing

After running the benchmarks above, we investigated why request distribution was perfectly equal across all pods. Equal distribution is what round-robin gives you, not what an intelligent scorer with prefix-cache awareness should produce. We discovered four separate issues that had to be fixed in sequence.

**Bug 1: Envoy's base ext_proc filter pointed to a dummy cluster.** When KServe creates the InferencePool and HTTPRoute, Istio registers a base `ext_proc` HTTP filter in the Gateway with `cluster_name: "dummy"` and `request_header_mode: SKIP`. This disables the ext_proc processing entirely. Our initial EnvoyFilter targeted `applyTo: HTTP_ROUTE` (per-route override), but per-route overrides cannot un-SKIP a base filter. The fix was changing the EnvoyFilter to target `applyTo: HTTP_FILTER` and replacing the dummy cluster with the real EPP service. After this fix, Envoy proxy metrics showed `cx_total` going from 0 to 5.

**Bug 2: TLS handshake failure between Envoy and EPP.** After Bug 1, Envoy was connecting to EPP but every connection failed (`cx_connect_fail: 5/5`). The EPP had a TLS certificate mounted at `/var/run/kserve/tls` but was not told to use it. The fix was patching the LLMInferenceService to add `--cert-path=/var/run/kserve/tls` and `--secure-serving` to the scheduler container args.

**Bug 3: Wrong EndpointPickerConfig (no P/D awareness).** The default EndpointPickerConfig uses a single `default` scheduling profile that treats all pods identically. For P/D disaggregation, the EPP needs `pd-profile-handler` with separate `prefill` and `decode` profiles, plus `prefill-filter` and `decode-filter` plugins. We patched the LLMInferenceService with the correct P/D-aware config from the [official KServe P/D sample](https://github.com/red-hat-data-services/kserve/blob/main/docs/samples/llmisvc/single-node-gpu/llm-inference-service-pd-qwen2-7b-gpu.yaml).

**Bug 4: Prefix caching disabled on vLLM.** Even with EPP routing correctly, prefix cache hits were 0. vLLM's Automatic Prefix Caching is off by default in the v0 engine. We added `--enable-prefix-caching --block-size=16` to `VLLM_ADDITIONAL_ARGS` for both prefill and decode pods, and added `prefix-cache-scorer` (weight 3) to both scheduling profiles.

After all four fixes, re-running inference-perf showed the EPP was fully operational: 86% prefix cache hit rate, true P/D-aware routing (prefill pods handle prefill, decode pods handle decode), and session affinity (shared-prefix requests are concentrated on the pod with the warm cache).

| Phase | Pod Distribution | Cache Hits | EPP Status |
|-------|-----------------|------------|------------|
| Before fixes | 25 / 25 / 25 / 25 | 65% (local APC only) | Dead (dummy cluster) |
| After all fixes | Prefill 46/54, Decode 100/0 | **86%** | Full (P/D + cache-aware + session affinity) |

The full debugging walkthrough with exact commands and YAML is in our [EPP troubleshooting guide](https://github.com/nirjhar17/llm-d-observability-openshift/blob/main/llm-d-request-flow-guide.md#4-the-bug).

## Observability

To see what is happening inside the cluster during benchmark runs, we set up Grafana with Prometheus dashboards for both vLLM and EPP metrics. The full observability setup, including Grafana Operator installation, RBAC, datasource configuration, and pre-built dashboards, is covered in our [companion observability blog](https://github.com/nirjhar17/llm-d-observability-openshift).

## Reproducing This

All manifests, benchmark job definitions, and the parse script are in our repository:

> Repository: [github.com/nirjhar17/guidellm-pd-disaggregation](https://github.com/nirjhar17/guidellm-pd-disaggregation)

To reproduce this, we need an OpenShift cluster with RHOAI and GPU nodes, a model deployed via LLMInferenceService with P/D disaggregation, User Workload Monitoring enabled, and the Grafana Operator installed for dashboards.

The benchmark jobs can be applied with:

```
oc apply -f manifests/05-benchmark-profiles.yaml
```

## About Me

I work on OpenShift, OpenShift AI, and observability solutions, focusing on simplifying complex setups into practical, repeatable steps for platform and development teams.

GitHub: [github.com/nirjhar17](https://github.com/nirjhar17)

LinkedIn: [linkedin.com/in/nirjhar-jajodia](https://linkedin.com/in/nirjhar-jajodia)

## Disclaimer

The views and opinions expressed in this article are my own and do not necessarily reflect the official policy or position of my employer. This guide is provided for educational purposes, and I make no warranties about the completeness, reliability, or accuracy of this information.
