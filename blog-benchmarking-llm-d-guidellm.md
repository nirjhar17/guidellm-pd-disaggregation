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

The prefill pods receive the full prompt and process all tokens in parallel, building the KV cache. That cache is transferred to the decode pods, which generate output tokens one at a time. The EPP (Endpoint Picker) router scores every pod on queue depth, KV cache utilization, and prefix cache hits, then picks the best one for each request.

## The LLMInferenceService with P/D Disaggregation

The core of our deployment is a single LLMInferenceService CR that defines both the prefill and decode pods, along with the EPP scheduler. Here is the YAML we used to deploy the model with P/D disaggregation:

```
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

  # EPP Scheduler with scoring plugins
  router:
    gateway: {}
    route: {}
    scheduler:
      template:
        containers:
        - name: main
          args:
          - --config-text
          - |
            apiVersion: inference.networking.x-k8s.io/v1alpha1
            kind: EndpointPickerConfig
            plugins:
            - type: queue-scorer
            - type: kv-cache-utilization-scorer
            - type: prefix-cache-scorer
            schedulingProfiles:
            - name: default
              plugins:
              - pluginRef: queue-scorer
                weight: 2
              - pluginRef: kv-cache-utilization-scorer
                weight: 2
              - pluginRef: prefix-cache-scorer
                weight: 3
```

The spec.replicas controls the decode pod count, while spec.prefill.replicas controls prefill. The router section embeds the EPP scheduler configuration with the three scoring plugins. One YAML creates the entire disaggregated inference stack.

The full manifest with all fields (nodeSelector, tolerations, resource limits, env vars) is available at [manifests/01-llminferenceservice-pd.yaml](https://github.com/nirjhar17/guidellm-pd-disaggregation/blob/main/manifests/01-llminferenceservice-pd.yaml).

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

- Synchronous: 22 requests, 0.35 RPS, TTFT median 64.2ms, ITL median 21.4ms, latency median 2.74s
- Concurrent (4): 79 requests, 1.30 RPS, TTFT median 54.2ms, ITL median 23.7ms, latency median 3.02s
- Concurrent (16): 259 requests, 4.30 RPS, TTFT median 75.0ms, ITL median 28.2ms, latency median 3.60s
- Throughput (64): 758 requests, 12.63 RPS, TTFT median 118.3ms, ITL median 38.2ms, latency median 4.87s
- Constant (5/s): 281 requests, 4.68 RPS, TTFT median 75.2ms, ITL median 28.9ms, latency median 3.69s
- Poisson (5/s): 335 requests, 5.58 RPS, TTFT median 85.9ms, ITL median 29.7ms, latency median 3.79s
- Sweep (auto): 23-1081 requests, 0.37-15.58 RPS, TTFT median 63.3-134.6ms, ITL median 20.4-57.4ms

## What the Numbers Tell Us

Concurrent(4) achieves the lowest TTFT at 54.2ms median, even better than synchronous. With 4 requests in flight, the prefill pods stay warm and GPU utilization is steady without being overloaded.

At 12.63 RPS in throughput mode, the system handled 758 requests in 60 seconds with only 1 error. But TTFT doubled to 118ms and ITL nearly doubled to 38ms. That's the throughput ceiling.

The sweep profile makes the sweet spot visible. TTFT stays under 100ms up to about 9 RPS. Beyond that, latency starts climbing steeply:

- At sync (0.37 req/s): TTFT 63.3ms, ITL 20.4ms
- At 2.6 req/s: TTFT 73.3ms, ITL 26.9ms
- At 4.8 req/s: TTFT 78.2ms, ITL 28.6ms
- At 7.0 req/s: TTFT 82.3ms, ITL 29.7ms
- At 9.2 req/s: TTFT 87.2ms, ITL 31.9ms
- At 11.4 req/s: TTFT 94.7ms, ITL 35.6ms
- At 13.6 req/s: TTFT 102.7ms, ITL 39.8ms
- At 15.8 req/s: TTFT 115.9ms, ITL 47.0ms
- At 18.0 req/s (max): TTFT 134.6ms, ITL 57.4ms

For this model on this hardware, 7-9 RPS gives good throughput with acceptable latency. Beyond that, we are trading user experience for raw throughput.

At the same target rate of 5 req/s, Poisson generated more total requests (335 vs 281) but with higher median TTFT (85.9ms vs 75.2ms). The bursty arrival pattern creates momentary queuing spikes, which is exactly what real traffic does.

## Proving EPP Intelligent Routing

Running benchmarks is one thing. Proving the EPP router actually distributes requests across all pods is another. We used three methods.

The first method was checking vLLM pod request counts. During a running benchmark, we checked access logs on each pod:

```
for pod in $(oc get pods -n my-first-model -l app=isvc.qwen3-0-6b \
  -o name); do
  echo "=== $pod ==="
  oc logs $pod -n my-first-model -c main --since=30s | \
    grep "POST /v1/chat/completions" | wc -l
done
```

All 4 pods (2 prefill + 2 decode) received roughly equal request counts, about 350 each across the full benchmark session.

The second method was querying vLLM metrics directly. Each pod exposes Prometheus metrics at port 8000. We used oc exec to curl localhost:8000/metrics and grep for running_requests, waiting_requests, and gpu_cache_usage. This showed active request distribution, queue depths, and KV cache usage per pod in real time.

The third method was checking EPP Prometheus metrics. The EPP exposes its own metrics at port 9090, including inference_pool_per_pod_queue_size which shows queue depth per pod.

Every request follows the same path, confirmed by source IP analysis:

```
GuideLLM Job → External LB → Envoy Gateway (10.130.0.37)
  → EPP Scheduler (10.130.0.35) → Prefill/Decode Pods
```

All vLLM pod access logs showed the same source IP (the Envoy Gateway), confirming that EPP was in the routing chain for every request.

The EPP uses three scoring plugins configured in the EndpointPickerConfig:

- queue-scorer (weight 2): routes to pod with shortest queue
- kv-cache-utilization-scorer (weight 2): routes to pod with most free KV cache
- prefix-cache-scorer (weight 3): routes to pod that already cached the prompt prefix

Prefix cache gets the highest weight because a cache hit saves the entire prefill phase, a significant latency reduction.

## GuideLLM Output Formats

GuideLLM generates four output formats: HTML for visual charts, JSON for the full authoritative record with all percentiles and per-request timings, YAML for human-readable inspection, and CSV for spreadsheets. Use --outputs to request any combination, for example: --outputs "results.json,results.html,results.csv"

## Comparing With and Without P/D Disaggregation

Before enabling P/D disaggregation, we ran the same GuideLLM sweep benchmark against a standard KServe deployment of the same Qwen3-0.6B model on the same hardware. That benchmark targeted the vLLM workload service directly with no EPP routing and no prefill/decode split. The full walkthrough of that benchmark is in our earlier blog: [Exploring GuideLLM: Benchmarking a Live LLM on OpenShift](https://medium.com/@jajodia.nirjhar/exploring-guidellm-benchmarking-a-live-llm-on-openshift-ccc2d0841794).

Here is what changed when we enabled P/D disaggregation.

Maximum throughput went from 8.53 RPS to 18.0 RPS. That is a 2.1x improvement. The standard deployment hit its ceiling with a single vLLM pod handling both prefill and decode on one GPU. With P/D, the work is split across 4 pods (2 prefill + 2 decode) on 2 GPUs, and the EPP routes each request to the least loaded pod.

The saturation point shifted from 5-6 RPS to 7-9 RPS. In the standard deployment, the sweep graph showed latency exploding around 5-6 RPS, the "knee" where user experience degrades. With P/D disaggregation, that knee moved to 7-9 RPS. The "green zone" where latency stays flat and predictable is significantly wider.

Baseline TTFT is higher with P/D: 63ms vs 32ms at low load. This is the routing overhead. Every request now travels through the Envoy Gateway, then to the EPP which scores all available pods on queue depth, KV cache utilization, and prefix cache hits, then forwards to the selected pod. At idle, that extra hop adds about 30ms. But this trade-off pays for itself under load because the standard deployment was already at degraded TTFT by the time it hit 6 RPS, while the P/D setup maintains sub-100ms TTFT all the way to 9 RPS.

Inter-Token Latency was nearly identical in both setups: 19.74ms without P/D vs 20.4ms with P/D at low load. This makes sense because ITL is determined by the vLLM engine and GPU speed during the decode phase, not the routing layer. The EPP only routes the initial request. Once token generation starts, it streams directly from the decode pod to the client.

Request latency at low load: 2.54s without P/D vs 2.74s with P/D. The 200ms difference comes from the same routing overhead that affects TTFT. At high load, this gap reverses because the P/D setup handles queuing and contention much better with 4 pods instead of 1.

We also checked the vLLM pod logs during the P/D benchmark and found prefix cache hit rate was 0% across all pods. This is expected because GuideLLM generates random synthetic prompts with no shared prefixes. In a production chatbot or RAG workload where requests share a common system prompt, the prefix-cache-scorer (the highest weighted EPP plugin at weight 3) would start routing requests to pods that already cached those prefixes, further reducing TTFT.

KV cache utilization peaked at 31% during the heaviest sweep loads, well within our 40% VRAM budget. No pod hit the ceiling, no requests waited in queue, and no OOMs occurred. The headroom means this setup can handle burst traffic beyond the sustained maximum.

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
