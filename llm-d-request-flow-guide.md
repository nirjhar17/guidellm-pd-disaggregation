# llm-d: The Full Picture -- From Installing RHOAI to Serving Your Model

> This guide explains the ENTIRE journey: what happens when you install
> OpenShift AI, what gets created at each step, how requests reach your LLM,
> and how all the pieces connect. Written in plain English.

---

## Table of Contents

0. [The Full Journey -- From RHOAI Install to MaaS Gateway](#0-full-journey)
1. [The Simple Version -- What Is All This Stuff?](#1-simple-version)
2. [Your 3 Gateways -- Why Do I Have Three?](#2-your-3-gateways)
   - [Way 1: openshift-ai-inference (testing)](#way-1)
   - [Way 2: maas-gateway (production)](#way-2)
   - [Way 3: Direct to vLLM (debugging)](#way-3)
3. [How a Request Travels -- Step by Step](#3-request-flow)
4. [The Bug We Found and Fixed](#4-the-bug)
5. [How to Check Each Layer (Debugging)](#5-debugging)
6. [Copy-Paste Commands](#6-commands)

---

## 0. The Full Journey -- From RHOAI Install to MaaS Gateway <a name="0-full-journey"></a>

This section tells the complete story. Read it top to bottom and you'll understand
how everything connects.

### Step 1: You Start with a Bare ROSA Cluster

You have an OpenShift cluster on AWS (ROSA). It has:
- 4 CPU worker nodes (`m6a.2xlarge`)
- A GPU node pool configured in ROSA (auto-scales `g4dn.xlarge` nodes with Tesla T4 GPUs, max 2 nodes)
- The built-in OpenShift Router (`router-default`) that gives you nice URLs like `*.apps.rosa.openshiftai3.5zpy.p3.openshiftapps.com`

At this point, you can deploy regular apps but there's nothing AI-related.

### Step 2: Install the NVIDIA GPU Operator

**What you do**: Install "NVIDIA GPU Operator" from OperatorHub.

**What it creates**:
- Installs GPU drivers on every GPU node (compiles them for your kernel)
- Runs a device plugin that tells Kubernetes "this node has 1 GPU available"
- Runs DCGM exporter for GPU metrics
- Now Kubernetes knows about GPUs and can schedule pods that request `nvidia.com/gpu`

**After this step**: GPU nodes can run GPU workloads, but there's no AI platform yet.

### Step 3: Install OpenShift AI (RHOAI)

**What you do**: Install "Red Hat OpenShift AI" operator from OperatorHub.

**What it creates** (a LOT of things, automatically):

```
RHOAI Operator (rhods-operator)
│
├── Installs Istio/Service Mesh 3 (OSSM3)
│   └── Creates an Istio instance (v1.26.2)
│       └── This is the engine that runs Envoy proxy pods
│
├── Creates GatewayClass: "data-science-gateway-class"
│   └── Creates Gateway: "data-science-gateway"
│       └── AWS creates an ELB (Load Balancer) for it
│       └── This gateway serves: RHOAI Dashboard, Jupyter Notebooks, Pipelines
│
├── Creates GatewayClass: "openshift-ai-inference"
│   └── Creates Gateway: "openshift-ai-inference"
│       └── AWS creates ANOTHER ELB for it
│       └── This gateway is for: model inference (your LLMs)
│       └── NO authentication on this gateway (open to anyone with the URL)
│
├── Creates GatewayConfig: "default-gateway"
│   └── Configures TLS certificates, ingress mode (LoadBalancer)
│
├── Installs KNative Serving (via Serverless Operator)
├── Installs Authorino (auth engine, for later use by Kuadrant)
├── Installs Limitador (rate limiting engine, for later use by Kuadrant)
├── Creates the RHOAI Dashboard (in redhat-ods-applications namespace)
└── Sets up Model Registry, Notebook Controller, etc.
```

**The key thing to understand**: At this point you already have TWO gateways and TWO AWS load balancers, even though you haven't deployed any model yet.

```
                    ┌──────────────────────────────┐
Internet ──────────>│ ELB #1 (data-science-gateway) │──> RHOAI Dashboard, Notebooks
                    └──────────────────────────────┘
                    ┌──────────────────────────────┐
Internet ──────────>│ ELB #2 (openshift-ai-inference)│──> (empty, no models yet)
                    └──────────────────────────────┘
```

**Why are the URLs ugly ELB names?**
Traditional OpenShift Routes go through the built-in `router-default` which has a wildcard DNS
(`*.apps.rosa.openshiftai3...`), giving nice URLs. But these inference Gateways use the
**Gateway API** (a newer Kubernetes standard) which creates separate LoadBalancer Services.
Each one gets its own raw AWS ELB hostname like `aed7822c...elb.amazonaws.com`.
In production, you'd add a DNS CNAME to give it a nice name.

### Step 4: Deploy Your Model (LLMInferenceService)

**What you do**: Create an `LLMInferenceService` CR for Qwen3-0.6B in namespace `my-first-model`.

**What the operator creates automatically**:

```
LLMInferenceService (qwen3-0-6b)
│
├── Deployment: qwen3-0-6b-kserve (2 replicas)
│   └── vLLM pods that actually run your model
│   └── Each pod needs 1 GPU, so 2 pods = 2 GPU nodes
│
├── Deployment: qwen3-0-6b-kserve-router-scheduler (1 replica)
│   └── The EPP (Endpoint Picker) -- the "smart router"
│   └── Runs on a CPU node (doesn't need GPU)
│   └── Listens on port 9002 (gRPC) for routing decisions
│
├── Service: qwen3-0-6b-kserve-workload-svc (port 8000)
│   └── Direct access to vLLM pods (round-robin, no smart routing)
│
├── Service: qwen3-0-6b-epp-service (ports 9002, 9003, 9090)
│   └── 9002 = gRPC ext-proc (Envoy talks to EPP here)
│   └── 9003 = health checks
│   └── 9090 = Prometheus metrics
│
├── InferencePool: qwen3-0-6b-inference-pool
│   └── Groups the vLLM pods + points to the EPP service
│   └── Istio auto-creates a headless Service for direct pod routing
│
├── HTTPRoute: qwen3-0-6b-kserve-route
│   └── Attached to the "openshift-ai-inference" gateway
│   └── Says: /my-first-model/qwen3-0-6b/v1/chat/completions → InferencePool
│   └── Says: /my-first-model/qwen3-0-6b/v1/completions → InferencePool
│   └── Says: /my-first-model/qwen3-0-6b/* (catch-all) → direct Service
│
├── DestinationRules (x2)
│   └── TLS settings for Envoy-to-vLLM and Envoy-to-EPP connections
│
└── ServiceMonitor: kserve-llm-isvc-scheduler
    └── Tells Prometheus to scrape EPP metrics on port 9090
```

**After this step**: Your model is live! You can curl it at:
```
https://aed7822c...elb.amazonaws.com/my-first-model/qwen3-0-6b/v1/chat/completions
```

The architecture now looks like:

```
                    ┌──────────────────────────────────┐
Internet ──────────>│ ELB (openshift-ai-inference)      │
                    │         ↓                         │
                    │  Envoy (Gateway pod)              │
                    │         ↓                         │
                    │  ext-proc filter ──→ EPP pod      │
                    │         ↓            (picks best  │
                    │         ↓             vLLM pod)   │
                    │    ┌────┴────┐                    │
                    │    ↓         ↓                    │
                    │ vLLM #1   vLLM #2                │
                    │ (GPU 1)   (GPU 2)                │
                    └──────────────────────────────────┘
```

**No auth here!** Anyone who knows the ELB URL can use your model for free.

### Step 5: Set Up MaaS Gateway (You Do This Manually)

**Why**: The `openshift-ai-inference` gateway has NO authentication. For production,
you want API keys and rate limiting. That's what the MaaS (Model as a Service) setup gives you.

**What you do** (following the MaaS blog post):

**5a. Create the `maas-gateway`:**
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: maas-gateway
  namespace: openshift-ingress
  annotations:
    opendatahub.io/managed: "false"    # <-- Tell RHOAI: "don't touch this"
spec:
  gatewayClassName: openshift-ai-inference   # <-- Same class as the platform gateway
  listeners:
    - name: https
      port: 443
      protocol: HTTPS
      tls:
        certificateRefs:
          - name: maas-gateway-tls     # <-- Your TLS cert
```

This creates a THIRD AWS ELB. Both `openshift-ai-inference` and `maas-gateway` use the
same `gatewayClassName`, so they share the same Istio infrastructure underneath.

**5b. Create an HTTPRoute pointing to the same model:**
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: maas-model-route
  namespace: my-first-model
spec:
  parentRefs:
    - name: maas-gateway             # <-- Attach to YOUR gateway, not the platform one
      namespace: openshift-ingress
  rules:
    # Same paths, same backends as the auto-created route
    - matches:
        - path: {type: PathPrefix, value: /my-first-model/qwen3-0-6b/v1/chat/completions}
      backendRefs:
        - kind: InferencePool
          name: qwen3-0-6b-inference-pool
    # ... etc
```

**5c. Create Kuadrant AuthPolicy (requires API key):**
```yaml
apiVersion: kuadrant.io/v1
kind: AuthPolicy
metadata:
  name: maas-gateway-auth-policy
spec:
  targetRef:
    kind: Gateway
    name: maas-gateway
  # Configures: "You must provide a valid API key to use this gateway"
```

**5d. Create Kuadrant RateLimitPolicy (limits requests per user):**
```yaml
apiVersion: kuadrant.io/v1
kind: RateLimitPolicy
metadata:
  name: maas-gateway-rate-limit
spec:
  targetRef:
    kind: Gateway
    name: maas-gateway
  # Configures: "Max N requests per minute per API key"
```

**5e. Create API key tiers** (in namespaces like `tier-free`, `tier-premium`, `tier-enterprise`):
These define different rate limits for different users.

**After this step**: You now have TWO doors to the same model:

```
                   NO AUTH (for testing)                 WITH AUTH (for production)
                   ┌─────────────────────┐               ┌─────────────────────┐
Internet ─────────>│ openshift-ai-inference│  Internet ──>│ maas-gateway         │
                   │ ELB: aed7822c...    │               │ ELB: a08cb63a...    │
                   └────────┬────────────┘               └────────┬────────────┘
                            │                                      │
                            │         ┌───────────────┐           │
                            └────────>│ Same Envoy    │<──────────┘
                                      │ Same EPP      │
                                      │ Same vLLM pods│
                                      └───────────────┘
```

### Step 6: What We Added (Observability + Bug Fix)

**6a. Grafana dashboards** (in namespace `llm-d-monitoring`):
- Installed Grafana Operator
- Created Grafana instance with Thanos Querier datasource
- 2 dashboards: EPP routing metrics + vLLM performance metrics

**6b. EnvoyFilter bug fix**:
- Found that Istio 1.26.2 strips the request body when routing through EPP
- POST requests to `/v1/chat/completions` were failing with `400 "body required"`
- Applied an EnvoyFilter to change body mode from `FULL_DUPLEX_STREAMED` to `BUFFERED`
- Currently only applied to `openshift-ai-inference` (not yet to `maas-gateway`)

### The Complete Picture

Here is EVERYTHING on your cluster and how it connects:

```
┌─────────────────────────────────────────────────────────────────────────┐
│ YOUR ROSA CLUSTER (OCP 4.20.6, ap-southeast-1)                        │
│                                                                         │
│  OPERATORS INSTALLED:                                                   │
│  ┌──────────────────┐ ┌───────────────┐ ┌───────────────┐              │
│  │ RHOAI 3.2.0      │ │ OSSM3 3.2.1   │ │ GPU Op 25.10  │              │
│  │ (orchestrates    │ │ (runs Istio   │ │ (GPU drivers  │              │
│  │  everything)     │ │  v1.26.2)     │ │  + plugin)    │              │
│  └──────────────────┘ └───────────────┘ └───────────────┘              │
│  ┌──────────────────┐ ┌───────────────┐ ┌───────────────┐              │
│  │ Grafana Op 5.21  │ │ Authorino     │ │ Limitador     │              │
│  │ (dashboards)     │ │ (auth engine) │ │ (rate limits) │              │
│  └──────────────────┘ └───────────────┘ └───────────────┘              │
│                                                                         │
│  NODES:                                                                 │
│  ┌──────────────────────────────────────────────────────────┐          │
│  │ 4x m6a.2xlarge (CPU)    │ 2x g4dn.xlarge (GPU, T4)     │          │
│  │ - EPP pod               │ - vLLM pod #1 (10.129.18.21)  │          │
│  │ - Grafana               │ - vLLM pod #2 (10.131.20.20)  │          │
│  │ - LlamaStack playground │                                │          │
│  └──────────────────────────────────────────────────────────┘          │
│                                                                         │
│  3 GATEWAYS (each = its own ELB on AWS):                               │
│                                                                         │
│  ┌───────────────────────┐  Created by: RHOAI platform                 │
│  │ data-science-gateway  │  For: Dashboard, Notebooks                  │
│  │ ELB: a45138ea...      │  You don't use this for models              │
│  └───────────────────────┘                                              │
│                                                                         │
│  ┌───────────────────────┐  Created by: RHOAI (via GatewayConfig)      │
│  │ openshift-ai-inference│  For: Model inference (testing)             │
│  │ ELB: aed7822c...      │  Auth: NONE                                │
│  │ EnvoyFilter: APPLIED  │  HTTPRoute: auto-created by operator       │
│  └───────────┬───────────┘                                              │
│              │                                                          │
│              │  ┌───────────────────────┐  Created by: YOU (MaaS blog) │
│              │  │ maas-gateway          │  For: Model inference (prod) │
│              │  │ ELB: a08cb63a...      │  Auth: Kuadrant (API key)    │
│              │  │ EnvoyFilter: NOT YET  │  HTTPRoute: you created      │
│              │  └───────────┬───────────┘                               │
│              │              │                                           │
│              └──────┬───────┘                                           │
│                     │  Both gateways route to the SAME backend:        │
│                     ▼                                                   │
│  ┌──────────────────────────────────────────────────┐                  │
│  │  namespace: my-first-model                        │                  │
│  │                                                    │                  │
│  │  InferencePool ──→ EPP (port 9002)                │                  │
│  │       │              scores pods, picks best one   │                  │
│  │       ▼                                            │                  │
│  │  ┌─────────┐  ┌─────────┐                         │                  │
│  │  │ vLLM #1 │  │ vLLM #2 │  (Qwen/Qwen3-0.6B)    │                  │
│  │  │ GPU 1   │  │ GPU 2   │                         │                  │
│  │  └─────────┘  └─────────┘                         │                  │
│  └──────────────────────────────────────────────────┘                  │
│                                                                         │
│  MONITORING (namespace: llm-d-monitoring):                             │
│  ┌──────────────────────────────────────────────────┐                  │
│  │  Grafana ──→ Thanos Querier ──→ Prometheus       │                  │
│  │  2 dashboards: EPP routing + vLLM performance    │                  │
│  └──────────────────────────────────────────────────┘                  │
│                                                                         │
│  OTHER NAMESPACES:                                                     │
│  fine-tune, guidellm-lab, lmeval-testing, nemo-evaluator,             │
│  rag-pipeline, maas-api, tier-free/premium/enterprise                  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Quick Summary: What Creates What

| Step | What You Do | What Gets Created Automatically |
|------|------------|-------------------------------|
| 1 | Install GPU Operator | GPU drivers, device plugin on GPU nodes |
| 2 | Install RHOAI | Istio, 2 GatewayClasses, 2 Gateways, 2 ELBs, Dashboard, KNative |
| 3 | Create LLMInferenceService | vLLM pods, EPP pod, Services, InferencePool, HTTPRoute, DestinationRules |
| 4 | Create maas-gateway (manual) | 3rd ELB |
| 5 | Create HTTPRoute for maas (manual) | Route from maas-gateway to same InferencePool |
| 6 | Create AuthPolicy (manual) | Kuadrant injects auth EnvoyFilter |
| 7 | Create RateLimitPolicy (manual) | Kuadrant injects rate-limit EnvoyFilter |
| 8 | Install Grafana Operator + setup | Grafana pod, datasource, dashboards |
| 9 | Apply EnvoyFilter fix (manual) | Fixes body forwarding for EPP on Istio 1.26 |

**Steps 1-3 give you a working model with no auth.**
**Steps 4-7 add the production layer (auth + rate limiting).**
**Steps 8-9 add observability and fix a bug.**

---

## 1. The Simple Version -- What Is All This Stuff? <a name="1-simple-version"></a>

Imagine you're ordering food from a restaurant chain that has multiple kitchens:

```
YOU (curl / browser / GuideLLM)
 |
 |  "I want a chat completion"
 v
GATEWAY = The front door. It receives your request from the internet.
 |
 |  "This request is for the Qwen model"
 v
EPP = The smart host. It looks at ALL kitchens and picks the
      best one (least busy, most cache, shortest queue).
 |
 |  "Send this to Kitchen #2, it has the prefix cached"
 v
vLLM POD = The kitchen. It actually runs your model and
           generates the tokens.
```

That's it. Three pieces: **Gateway** (front door), **EPP** (smart host), **vLLM** (kitchen).

### Every Term Explained Simply

| Term | Simple Explanation |
|------|-------------------|
| **Gateway** | A pod running Envoy proxy. It's the entry point from the internet. Creates an AWS load balancer URL you can curl. |
| **HTTPRoute** | A rule that says "if someone goes to URL X, send them to backend Y". Like a restaurant menu that says "pizza is in kitchen A, pasta is in kitchen B". |
| **Envoy** | The software inside the Gateway pod. It does the actual work of receiving, routing, and forwarding requests. You don't interact with it directly. |
| **InferencePool** | A group of model pods (your vLLM replicas) + a pointer to the EPP service. It tells the system "these pods serve the same model, use EPP to pick between them". |
| **EPP** | Endpoint Picker Protocol. A small gRPC server that Envoy asks "which pod should I send this to?" before every request. It knows each pod's queue depth, memory usage, and cache state. |
| **ext-proc** | External Processing filter. This is HOW Envoy talks to EPP. Envoy has a built-in feature called "ext-proc" that can send request data to an external server. That external server is EPP. |
| **vLLM** | Your model server. Runs the actual LLM. Exposes an OpenAI-compatible API at `/v1/chat/completions`. |
| **EnvoyFilter** | A Kubernetes resource that lets you modify Envoy's behavior without rebuilding it. Like a config patch. We used this to fix the bug. |
| **Headless Service** | A Kubernetes Service that doesn't have a single IP address. Instead, it gives you the individual IPs of each pod. This lets EPP route to a SPECIFIC pod, not just "any pod in the group". |

### How EPP Picks the Best Pod

EPP scores every vLLM pod on three things, then picks the one with the highest total score:

| What It Checks | Plain English | Score Weight |
|---------------|---------------|-------------|
| **Queue depth** | How many requests are waiting in line at this pod? Less = better. | 2 |
| **KV cache usage** | How full is this pod's GPU memory? Less full = better. | 2 |
| **Prefix cache** | Does this pod already have the user's prompt cached from a previous request? If yes, much faster! | 3 (most important) |

---

## 2. Your 3 Gateways -- Why Do I Have Three? <a name="2-your-3-gateways"></a>

This is confusing because you have **3 front doors** to the same building. Here's why:

```
YOUR CLUSTER
│
├── data-science-gateway ......... For the RHOAI Dashboard, Notebooks, Pipelines.
│                                  NOT for your model. You never send model
│                                  requests here. Ignore it for inference.
│
├── openshift-ai-inference ....... Created automatically by the LLMInferenceService
│                                  operator when you deployed your model.
│                                  NO authentication. Open to anyone with the URL.
│                                  Good for testing.
│
└── maas-gateway ................. Created by YOU (from the MaaS blog post).
                                   HAS authentication (Kuadrant AuthPolicy).
                                   HAS rate limiting (Kuadrant RateLimitPolicy).
                                   This is your "production" endpoint.
```

### Side by Side

|  | data-science-gateway | openshift-ai-inference | maas-gateway |
|--|---------------------|----------------------|-------------|
| **Created by** | RHOAI platform | LLMInferenceService operator | You (manually) |
| **What it's for** | Dashboard, notebooks | Testing model access | Production model access |
| **Serves your model?** | No | **Yes** | **Yes** |
| **Needs auth?** | Yes (OAuth) | **No** (open!) | **Yes** (API key) |
| **Uses EPP?** | No | **Yes** | **Yes** |
| **Same vLLM pods?** | N/A | **Yes** (same 2 pods) | **Yes** (same 2 pods) |

### The Key Point

`openshift-ai-inference` and `maas-gateway` are **two different doors into the same room**. They both reach the same InferencePool, the same EPP, and the same vLLM pods. The only difference is authentication.

For testing, use `openshift-ai-inference` (no auth needed, just curl it).
For real users, use `maas-gateway` (has API key protection).

### How to Access Your Model -- 3 Ways

There are **3 ways** to send requests to your model. Each one has a different URL and different behavior. Here's how to find and use each one.

---

#### Way 1: Through `openshift-ai-inference` Gateway (easiest for testing)

**What is it?** A Gateway created automatically when you deploy your model. No authentication. Anyone with the URL can use it.

**How to find the URL:**

```bash
# Step 1: Get the gateway's external address (this is an AWS load balancer)
oc get gateway openshift-ai-inference -n openshift-ingress \
  -o jsonpath='{.status.addresses[0].value}'
```

On our cluster, this returns:
```
aed7822c4ec88495fa795e5c47c2d24f-1452512295.ap-southeast-1.elb.amazonaws.com
```

**How to build the full URL:**

The URL is: `https://<gateway-address>/<namespace>/<model-name>/v1/<endpoint>`

On our cluster:
- Namespace = `my-first-model` (where the LLMInferenceService lives)
- Model name = `qwen3-0-6b` (the Kubernetes name, NOT the HuggingFace name)

So the URLs are:

```bash
# Save the gateway address
export GW=$(oc get gateway openshift-ai-inference -n openshift-ingress \
  -o jsonpath='{.status.addresses[0].value}')

# Chat completion (goes through EPP -- smart routing)
curl -sk -X POST "https://$GW/my-first-model/qwen3-0-6b/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hi"}],"max_tokens":20}'

# Text completion (also goes through EPP)
curl -sk -X POST "https://$GW/my-first-model/qwen3-0-6b/v1/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","prompt":"Hello world","max_tokens":20}'

# List models (does NOT go through EPP, goes direct to vLLM)
curl -sk "https://$GW/my-first-model/qwen3-0-6b/v1/models"
```

**How to figure out the path prefix (`/my-first-model/qwen3-0-6b`)?**

The path comes from the HTTPRoute. To see all valid paths for all gateways:

```bash
oc get httproute -A -o json | python3 -c "
import sys, json
for item in json.load(sys.stdin)['items']:
    for pref in item['spec'].get('parentRefs', []):
        gw = pref.get('name')
        for rule in item['spec'].get('rules', []):
            for m in rule.get('matches', []):
                path = m.get('path', {}).get('value', '?')
                backends = ', '.join([f\"{b.get('kind','Svc')}/{b['name']}\" for b in rule.get('backendRefs', [])])
                print(f'  Gateway: {gw:30s} Path: {path:60s} -> {backends}')
"
```

On our cluster, this shows:
```
  Gateway: openshift-ai-inference        Path: /my-first-model/qwen3-0-6b/v1/chat/completions  -> InferencePool/qwen3-0-6b-inference-pool
  Gateway: openshift-ai-inference        Path: /my-first-model/qwen3-0-6b/v1/completions       -> InferencePool/qwen3-0-6b-inference-pool
  Gateway: openshift-ai-inference        Path: /my-first-model/qwen3-0-6b                      -> Service/qwen3-0-6b-kserve-workload-svc
  Gateway: maas-gateway                  Path: /my-first-model/qwen3-0-6b/v1/chat/completions  -> InferencePool/qwen3-0-6b-inference-pool
  Gateway: maas-gateway                  Path: /my-first-model/qwen3-0-6b/v1/completions       -> InferencePool/qwen3-0-6b-inference-pool
  Gateway: maas-gateway                  Path: /my-first-model/qwen3-0-6b                      -> Service/qwen3-0-6b-kserve-workload-svc
```

Notice: Both `openshift-ai-inference` and `maas-gateway` have the **same paths** and **same backends**. The only difference is the gateway address (different ELB URL) and authentication.

---

#### Way 2: Through `maas-gateway` (production, with authentication)

**What is it?** A Gateway you created manually (from the MaaS blog post). It has Kuadrant AuthPolicy (requires API key) and RateLimitPolicy.

**How to find the URL:**

```bash
oc get gateway maas-gateway -n openshift-ingress \
  -o jsonpath='{.status.addresses[0].value}'
```

On our cluster:
```
a08cb63af408d4d8daab533606935da9-1640345712.ap-southeast-1.elb.amazonaws.com
```

**How to use it:**

Same paths as `openshift-ai-inference`, but you need an API key:

```bash
export MAAS_GW=$(oc get gateway maas-gateway -n openshift-ingress \
  -o jsonpath='{.status.addresses[0].value}')

# This will return 401 Unauthorized (because no API key)
curl -sk "https://$MAAS_GW/my-first-model/qwen3-0-6b/v1/models"

# With API key (you need to set up Kuadrant auth first)
curl -sk -H "Authorization: Bearer <your-api-key>" \
  "https://$MAAS_GW/my-first-model/qwen3-0-6b/v1/models"
```

> **Note**: The EnvoyFilter body-forwarding fix currently only applies to `openshift-ai-inference`.
> To fix POST requests on `maas-gateway` too, you need a second EnvoyFilter (see `manifests/08-envoyfilter-fix-extproc-body.yaml`).

---

#### Way 3: Direct to vLLM Service (bypasses Gateway AND EPP)

**What is it?** You skip the Gateway entirely and talk directly to the vLLM Kubernetes Service from inside the cluster. No EPP smart routing, no Envoy, no authentication. Useful for debugging.

**How to find the service:**

```bash
oc get svc -n my-first-model
```

On our cluster:
```
NAME                                    TYPE        CLUSTER-IP       PORT(S)
qwen3-0-6b-kserve-workload-svc          ClusterIP   172.30.156.35    8000/TCP
qwen3-0-6b-epp-service                  ClusterIP   172.30.187.192   9002/TCP,9003/TCP,9090/TCP
qwen3-0-6b-inference-pool-ip-a5e07bf3   ClusterIP   None             54321/TCP
```

The service you want is `qwen3-0-6b-kserve-workload-svc` on port `8000`.

**Important**: This service uses `appProtocol: https`, so you must use HTTPS even from inside the cluster.

**How to use it (from inside the cluster only):**

You can't reach a ClusterIP service from your laptop. You need to be inside a pod, or use `oc port-forward`, or run curl from a debug pod.

```bash
# Option A: Port-forward to your laptop
oc port-forward svc/qwen3-0-6b-kserve-workload-svc 8000:8000 -n my-first-model &

# Then curl localhost (note: HTTPS because the vLLM service requires it)
curl -sk "https://localhost:8000/v1/models"
curl -sk -X POST "https://localhost:8000/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hi"}],"max_tokens":10}'

# Don't forget to kill the port-forward when done
kill %1
```

```bash
# Option B: Run curl from inside the cluster (using a debug pod)
oc run curl-test --rm -it --image=curlimages/curl --restart=Never -- \
  curl -sk "https://qwen3-0-6b-kserve-workload-svc.my-first-model.svc.cluster.local:8000/v1/models"
```

**Key differences when going direct:**
- No path prefix needed! It's just `/v1/models`, `/v1/chat/completions`, etc.
  (The gateway strips the `/my-first-model/qwen3-0-6b` prefix before forwarding.
   When you go direct, there's nothing to strip.)
- No EPP routing -- Kubernetes round-robins to any vLLM pod
- Must use HTTPS (vLLM has TLS enabled)
- Only works from inside the cluster (ClusterIP is not reachable from outside)

---

#### Summary: All 3 Ways at a Glance

```
                                  ┌──────────────────────────────────┐
YOUR LAPTOP                       │         YOUR CLUSTER             │
                                  │                                  │
Way 1: openshift-ai-inference     │                                  │
  https://<elb-1>/my-first-model/ │   ┌──────────┐   ┌─────┐        │
  qwen3-0-6b/v1/chat/completions ───> │ Gateway  │──>│ EPP │──┐     │
  (no auth, for testing)          │   └──────────┘   └─────┘  │     │
                                  │                            │     │
Way 2: maas-gateway               │                            ▼     │
  https://<elb-2>/my-first-model/ │   ┌──────────┐   ┌─────┐ ┌───┐  │
  qwen3-0-6b/v1/chat/completions ───> │ Gateway  │──>│ EPP │─>│vLLM│ │
  (needs API key)                 │   │ +AuthPol. │   └─────┘ │pods│ │
                                  │   └──────────┘           └───┘  │
Way 3: Direct (inside cluster)    │                            ▲     │
  https://qwen3-0-6b-kserve-     │                            │     │
  workload-svc:8000/              │   (no gateway, no EPP)     │     │
  v1/chat/completions ─────────────────────────────────────────┘     │
  (oc port-forward or from a pod) │                                  │
                                  └──────────────────────────────────┘
```

| | Way 1: openshift-ai-inference | Way 2: maas-gateway | Way 3: Direct Service |
|---|---|---|---|
| **URL prefix** | `/my-first-model/qwen3-0-6b/v1/...` | `/my-first-model/qwen3-0-6b/v1/...` | `/v1/...` (no prefix!) |
| **Protocol** | HTTPS | HTTPS | HTTPS |
| **Auth needed?** | No | Yes (API key) | No |
| **Goes through EPP?** | Yes (for completions) | Yes (for completions) | No (round-robin) |
| **Reachable from laptop?** | Yes (ELB URL) | Yes (ELB URL) | No (need port-forward) |
| **EnvoyFilter fix needed?** | Yes (applied) | Yes (not yet applied) | No (no Envoy involved) |
| **Good for** | Quick testing | Production users | Debugging vLLM directly |

---

## 3. How a Request Travels -- Step by Step <a name="3-request-flow"></a>

### The Path of a Chat Completion Request

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

### The 3 URL Rules (HTTPRoute)

Your model has 3 URL paths. Each takes a **different route**:

| URL Path | Goes Through EPP? | Backend |
|----------|-------------------|---------|
| `/my-first-model/qwen3-0-6b/v1/chat/completions` | **Yes** (InferencePool) | EPP picks the best vLLM pod |
| `/my-first-model/qwen3-0-6b/v1/completions` | **Yes** (InferencePool) | EPP picks the best vLLM pod |
| `/my-first-model/qwen3-0-6b/v1/models` (or anything else) | **No** (direct Service) | Round-robin to any vLLM pod |

This is why `/v1/models` (GET) always worked but `/v1/chat/completions` (POST) was broken -- they take different paths inside the cluster!

### Important: The Model Name

When you send a request to vLLM, the `"model"` field in your JSON body must match what vLLM registered. On our cluster:

- **Wrong**: `"model": "qwen3-0-6b"` (this is the Kubernetes name)
- **Right**: `"model": "Qwen/Qwen3-0.6B"` (this is the HuggingFace name vLLM uses)

To check: `curl -sk "https://$GW/my-first-model/qwen3-0-6b/v1/models"`

---

## 4. The Bugs We Found and Fixed <a name="4-the-bug"></a>

We found **four separate issues** that prevented EPP intelligent routing from working on RHOAI 3.2 with Istio 1.26.2. Each one had to be fixed in order — you could not skip ahead because the next issue only became visible after fixing the previous one.

### Bug 1: Request Body Disappearing (400 "body required")

**Symptom**: POST requests to `/v1/chat/completions` returned `400 "Field required"`.

**Root cause**: Istio 1.26.2 set `request_body_mode` to `FULL_DUPLEX_STREAMED` for the ext-proc filter. In this mode, the body was sent to EPP but never forwarded to vLLM.

**Fix**: Applied an EnvoyFilter to change body mode to `STREAMED`:

```bash
oc apply -f manifests/08-envoyfilter-fix-extproc-body.yaml
```

**Result**: `400 → 200 OK`. Requests started reaching vLLM. But we later discovered this EnvoyFilter was targeting `HTTP_ROUTE` level, which was not enough for EPP to actually work (see Bug 2).

### Bug 2: EPP Never Contacted (dummy cluster)

**Symptom**: Requests returned 200, but all 4 pods received exactly equal traffic (25/25/25/25). EPP scoring plugins had no effect. Changing scorer weights made no difference.

**How we found it**: Checked Envoy proxy metrics for the EPP cluster:

```bash
ENVOY_POD=$(oc get pods -n openshift-ingress \
  -l gateway.networking.k8s.io/gateway-name=openshift-ai-inference \
  -o jsonpath='{.items[0].metadata.name}')

oc exec -n openshift-ingress $ENVOY_POD -c istio-proxy -- \
  pilot-agent request GET /clusters | grep "epp.*cx_total"
```

**The result was `cx_total: 0`**. Envoy had never even attempted to connect to EPP. Every request was going through Envoy's default round-robin.

**Root cause**: When KServe creates the InferencePool and HTTPRoute, Istio registers a **base ext_proc HTTP filter** in the Gateway with:
- `cluster_name: "dummy"`
- `request_header_mode: SKIP`

The SKIP setting meant the ext_proc filter was disabled at the base level. Our EnvoyFilter from Bug 1 was targeting `applyTo: HTTP_ROUTE` (per-route override), but the base filter's SKIP mode takes precedence — per-route overrides cannot un-SKIP a base filter.

**Fix**: Changed the EnvoyFilter to target `applyTo: HTTP_FILTER` (base filter level) instead of `HTTP_ROUTE`, replacing the dummy cluster with the real EPP service:

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

**Result**: `cx_total` went from 0 to 5 (Envoy now connecting to EPP). But all 5 connections failed (see Bug 3).

### Bug 3: TLS Handshake Failure (CERTIFICATE_VERIFY_FAILED)

**Symptom**: After Bug 2 fix, Envoy connected to EPP but every connection failed: `cx_connect_fail: 5, cx_total: 5`.

**How we found it**: The DestinationRule for the EPP service expected a service-CA signed certificate (`mode: SIMPLE`, `caCertificates: /var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt`). But the EPP was presenting a self-signed certificate instead.

**Root cause**: The EPP scheduler container had the TLS certificate mounted at `/var/run/kserve/tls` but was NOT told to use it. The `--cert-path` argument was missing from its startup args.

**Fix**: Patched the LLMInferenceService to add TLS args to the scheduler container:

```bash
oc patch llminferenceservice qwen3-0-6b -n my-first-model --type='json' -p='[
  {"op":"add","path":"/spec/router/scheduler/template/containers/0/args/-","value":"--secure-serving"},
  {"op":"add","path":"/spec/router/scheduler/template/containers/0/args/-","value":"--cert-path"},
  {"op":"add","path":"/spec/router/scheduler/template/containers/0/args/-","value":"/var/run/kserve/tls"}
]'
```

**Result**: `cx_connect_fail: 0`. Requests returned HTTP 200 with `x-went-into-resp-headers: true` (EPP is in the loop). But distribution was still wrong (see Bug 4).

### Bug 4: Wrong EndpointPickerConfig (no P/D awareness)

**Symptom**: After Bug 3 fix, EPP was live but distribution was still near-equal across all 4 pods. Prefill and decode pods were treated identically.

**Root cause**: The default EndpointPickerConfig used a single `default` scheduling profile with `queue-scorer`, `kv-cache-utilization-scorer`, and `prefix-cache-scorer`. This config treats all pods the same — it does not know about prefill vs decode roles. For P/D disaggregation, you need `pd-profile-handler` with separate `prefill` and `decode` profiles.

**Fix**: Patched the LLMInferenceService to replace the EndpointPickerConfig:

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

Also added `--enable-prefix-caching --block-size=16` to vLLM's `VLLM_ADDITIONAL_ARGS` for both prefill and decode pods to enable Automatic Prefix Caching.

**Result**: Distribution changed from 25/25/25/25 (round-robin) to true P/D routing. With 100 shared-prefix requests:
- Prefill pods: 46 + 54 = 100 (all requests go through prefill first)
- Decode pods: 100 + 0 = 100 (session affinity — warm cache pod gets everything)
- Prefix cache hit rate: **86%** (close to the Red Hat blog's 87.4%)

### Summary: The Four Fixes

| Bug | Symptom | Root Cause | Fix |
|-----|---------|------------|-----|
| 1 | 400 "body required" | Body mode FULL_DUPLEX_STREAMED | EnvoyFilter: change to STREAMED |
| 2 | EPP never contacted (cx_total: 0) | Base ext_proc filter = dummy/SKIP | EnvoyFilter: target HTTP_FILTER, replace dummy with real EPP |
| 3 | TLS failure (cx_connect_fail: 100%) | Missing --cert-path on EPP | Patch LLMInferenceService: add --cert-path=/var/run/kserve/tls |
| 4 | No P/D routing (equal distribution) | Wrong EndpointPickerConfig | Patch LLMInferenceService: pd-profile-handler + prefill/decode profiles |

### Before and After

| Phase | Pod Distribution | Cache Hits | EPP Status |
|-------|-----------------|------------|------------|
| Before all fixes | 25 / 25 / 25 / 25 | 0% | Dead (dummy cluster) |
| After Bugs 1-3 fixed | Prefill 60/40, Decode 53/47 | 0% | Working (P/D routing) |
| After all 4 bugs fixed | Prefill 46/54, Decode 100/0 | **86%** | Full (P/D + cache-aware + session affinity) |

### Are These Fixes Permanent?

- The **EnvoyFilter** persists across pod restarts. When RHOAI upgrades to a version where ext_proc is wired correctly by default, it can be removed.
- The **LLMInferenceService patches** persist because they modify the CR spec directly. The KServe controller reconciles from the CR, so the scheduler deployment always gets the correct args.
- The **prefix caching flags** (`--enable-prefix-caching`, `--block-size=16`) persist in the `VLLM_ADDITIONAL_ARGS` env var.

---

## 5. How to Check Each Layer (Debugging) <a name="5-debugging"></a>

If something breaks in the future, check these layers in order.

### Check 1: Is the Gateway running?

```bash
oc get pods -A -l 'gateway.networking.k8s.io/gateway-name'
```

**Good output**: All pods show `1/1 Running`
**Bad output**: `CrashLoopBackOff` or `0/1`

### Check 2: Does the Gateway have an external URL?

```bash
oc get gateway -A
```

Look for an address in `STATUS`. If it says `<none>`, the load balancer wasn't created.

### Check 3: Are the routes attached?

```bash
oc get httproute -A
```

Every route should show `Accepted: True` in its status.

### Check 4: Is the InferencePool working?

```bash
oc get inferencepool.inference.networking.x-k8s.io -A
```

It should exist and show `Accepted: True`.

### Check 5: Is EPP running?

```bash
oc get pods -n my-first-model | grep scheduler
```

Should be `1/1 Running`. Check logs if not:

```bash
oc logs -n my-first-model deploy/qwen3-0-6b-kserve-router-scheduler --tail=20
```

**Good sign**: `gRPC server listening {"name": "ext-proc", "port": 9002}`

### Check 6: Are the vLLM pods running?

```bash
oc get pods -n my-first-model -l app.kubernetes.io/name=qwen3-0-6b
```

Should show your replicas as `Running`.

### Check 7: Is the EnvoyFilter applied AND targeting HTTP_FILTER?

```bash
oc get envoyfilter -n openshift-ingress
```

Should show `fix-extproc-body-mode`. Verify it targets `HTTP_FILTER` (not `HTTP_ROUTE`):

```bash
oc get envoyfilter fix-extproc-body-mode -n openshift-ingress -o yaml | grep applyTo
```

Expected output: `applyTo: HTTP_FILTER`. If it says `HTTP_ROUTE`, the EnvoyFilter is not fixing the base ext_proc filter and EPP will not be contacted.

### Check 8: Is Envoy actually connecting to EPP?

This is the most important check for intelligent routing. If this fails, EPP is dead and traffic uses round-robin.

```bash
ENVOY_POD=$(oc get pods -n openshift-ingress \
  -l gateway.networking.k8s.io/gateway-name=openshift-ai-inference \
  -o jsonpath='{.items[0].metadata.name}')

oc exec -n openshift-ingress $ENVOY_POD -c istio-proxy -- \
  pilot-agent request GET /clusters | grep "epp-service"
```

Key metrics to look for:

| Metric | Good | Bad | Meaning |
|--------|------|-----|---------|
| `cx_total` | > 0 | 0 | 0 = Envoy never tried to connect (dummy cluster, Bug 2) |
| `cx_connect_fail` | 0 | > 0 | Connections attempted but TLS failed (Bug 3) |
| `rq_total` | matches your request count | 0 | Requests sent through EPP |
| `rq_error` | 0 | > 0 | EPP returned errors |

### Check 9: Is the EPP using the correct TLS cert?

```bash
oc get pods -n my-first-model -l app=qwen3-0-6b-kserve-router-scheduler \
  -o jsonpath='{.items[0].spec.containers[0].args}' | python3 -m json.tool
```

You should see `--cert-path`, `/var/run/kserve/tls`, and `--secure-serving` in the args. If missing, EPP presents a self-signed cert and Envoy will reject it.

### Check 10: Is the EndpointPickerConfig correct for P/D?

```bash
oc get pods -n my-first-model -l app=qwen3-0-6b-kserve-router-scheduler \
  -o jsonpath='{.items[0].spec.containers[0].args}' | python3 -m json.tool
```

Look for `pd-profile-handler`, `prefill-filter`, and `decode-filter` in the config. If you only see a single `default` profile with `queue-scorer`, the EPP is not P/D-aware.

### Check 11: Is prefix caching enabled on vLLM?

```bash
oc get pods -n my-first-model -l serving.kserve.io/inferenceservice=qwen3-0-6b \
  -o jsonpath='{range .items[*]}{.metadata.name}: {.spec.containers[0].env}' | grep -o "enable-prefix-caching"
```

If no output, vLLM is running with prefix caching disabled (default). Add `--enable-prefix-caching --block-size=16` to `VLLM_ADDITIONAL_ARGS`.

### Check 12: Verify prefix cache hits

```bash
# Pick a vLLM pod
POD=$(oc get pods -n my-first-model -l serving.kserve.io/inferenceservice=qwen3-0-6b \
  -o jsonpath='{.items[0].metadata.name}')

oc exec -n my-first-model $POD -- curl -s localhost:8000/metrics | grep prefix_cache
```

Expected: `vllm:prefix_cache_hits_total` should be non-zero after sending shared-prefix requests.

### Check 13: Quick end-to-end test

```bash
export GW=$(oc get gateway openshift-ai-inference -n openshift-ingress \
  -o jsonpath='{.status.addresses[0].value}')

# Does vLLM respond? (bypasses EPP)
curl -sk "https://$GW/my-first-model/qwen3-0-6b/v1/models"

# Does chat completion work? (goes through EPP)
curl -sk -X POST "https://$GW/my-first-model/qwen3-0-6b/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hi"}],"max_tokens":5}'
```

If the first works but the second gives `400 "body required"`, the EnvoyFilter is missing.
If the second gives `404 "model does not exist"`, the model name is wrong (check `/v1/models` output).
If it returns 200 but `x-went-into-resp-headers` header is missing, EPP is not in the loop.

### Check 9: The Envoy access log (most useful for debugging)

```bash
GWPOD=$(oc get pods -n openshift-ingress \
  -l gateway.networking.k8s.io/gateway-name=openshift-ai-inference \
  -o jsonpath='{.items[0].metadata.name}')
oc logs -n openshift-ingress $GWPOD -c istio-proxy --tail=5
```

Each line is one request. Here's how to read the important parts:

```
[timestamp] "POST /v1/chat/completions HTTP/2" 200 - via_upstream - "-" 106 656 694
                                                ^^^                     ^^^     ^^^
                                           HTTP status              body size  time(ms)
```

- `200` = success
- `400` = body was stripped (EnvoyFilter missing)
- `404` = wrong model name
- `401` = auth required (you're hitting maas-gateway, use openshift-ai-inference)

The access log also shows which vLLM pod handled the request:

```
"10.128.16.25:8000" outbound|54321||...inference-pool-ip-...
 ^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
 The specific pod   Went through InferencePool (EPP routing)
```

If you see `outbound|8000||...kserve-workload-svc...` instead, the request went to the direct Service (no EPP). This happens for `/v1/models` and other catch-all paths.

---

## 6. Copy-Paste Commands <a name="6-commands"></a>

### Set Up Your Environment

```bash
# Gateway URL (no auth)
export GW=$(oc get gateway openshift-ai-inference -n openshift-ingress \
  -o jsonpath='{.status.addresses[0].value}')
echo "Gateway: https://$GW"
```

### Test the Model

```bash
# Health check
curl -sk "https://$GW/my-first-model/qwen3-0-6b/v1/models" | python3 -m json.tool

# Chat completion
curl -sk -X POST "https://$GW/my-first-model/qwen3-0-6b/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello!"}],"max_tokens":20}' \
  | python3 -m json.tool
```

### Check Versions

```bash
oc get istio -A                    # Istio version (e.g., v1.26.2)
oc get csv -n openshift-operators | grep servicemesh3  # OSSM3 operator version
oc get csv -n redhat-ods-operator | grep rhods         # RHOAI version
```

### Check All Resources for Your Model

```bash
oc get pods,svc,deploy -n my-first-model
oc get inferencepool.inference.networking.x-k8s.io -n my-first-model
oc get httproute -n my-first-model
oc get envoyfilter -n openshift-ingress
```

### Watch Logs (Open 3 Terminals)

```bash
# Terminal 1: Envoy (shows every request)
GWPOD=$(oc get pods -n openshift-ingress \
  -l gateway.networking.k8s.io/gateway-name=openshift-ai-inference \
  -o jsonpath='{.items[0].metadata.name}')
oc logs -f -n openshift-ingress $GWPOD -c istio-proxy

# Terminal 2: EPP (shows routing decisions)
oc logs -f -n my-first-model deploy/qwen3-0-6b-kserve-router-scheduler

# Terminal 3: Send a request
curl -sk -X POST "https://$GW/my-first-model/qwen3-0-6b/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hi"}],"max_tokens":5}'
```

---

## Appendix: What the LLMInferenceService Creates Automatically

When you deploy a model using `LLMInferenceService`, the operator creates ALL of these for you:

```
LLMInferenceService (qwen3-0-6b)           <-- You create ONLY this
    |
    |-- Deployment (qwen3-0-6b-kserve)               = vLLM pods (your model)
    |-- Deployment (qwen3-0-6b-kserve-router-scheduler) = EPP pod (smart router)
    |-- Service (qwen3-0-6b-kserve-workload-svc)     = Direct access to vLLM
    |-- Service (qwen3-0-6b-epp-service)              = EPP gRPC service
    |-- InferencePool (qwen3-0-6b-inference-pool)     = Groups vLLM pods + EPP
    |      |-- Headless Service (auto-created by Istio)
    |-- HTTPRoute (qwen3-0-6b-kserve-route)           = URL routing rules
    |-- DestinationRule (x2)                           = TLS settings
```

You don't need to create any of these manually. However, on RHOAI 3.0–3.2 (Istio 1.26.x), you may need to manually:
- Apply an **EnvoyFilter** (`applyTo: HTTP_FILTER`) to fix the ext_proc wiring (dummy cluster → real EPP)
- Patch the **LLMInferenceService** to add `--cert-path=/var/run/kserve/tls` and `--secure-serving` to the scheduler
- Patch the **LLMInferenceService** to use the correct P/D-aware `EndpointPickerConfig` with `pd-profile-handler`
- Add `--enable-prefix-caching --block-size=16` to vLLM's `VLLM_ADDITIONAL_ARGS` for prefix cache hits
- Optionally create a **maas-gateway HTTPRoute** (if you want auth/rate-limiting via Kuadrant)
