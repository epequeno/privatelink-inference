# privatelink-inference

Most "private" model endpoints are private by convention — a security group, a
private subnet, and hope. This one is private by construction: a self-hosted
inference server behind an **internal NLB** and a **VPC endpoint service**, reached
from a separate VPC through an **interface endpoint**. There is no public route to
it, and the repo proves that rather than asserting it.

The same model is also hosted two ways, and both are measured. A self-hosted
llama.cpp host on EC2, and a **SageMaker** real-time endpoint trained by a real
SageMaker Training Job, registered in **Model Registry**, and reached from the
same internet-less VPC through the `sagemaker.runtime` interface endpoint.

> ### Measured 2026-10-18 — same model, two hosting paths
>
> | | llama.cpp on `c6i.large` | SageMaker `ml.g5.xlarge` |
> |---|---|---|
> | p50 latency | 147.5 ms | **68.5 ms** |
> | tokens/sec | 19.6 | **144.4** |
> | $/1k requests | **$0.0052** | $0.0268 |
>
> SageMaker row: trained by a real Training Job (**289 billable seconds**),
> registered in Model Registry, endpoint deployed from that entry (**342 s** to
> `InService`). Both reached from a VPC with no internet route; inside it,
> `runtime.sagemaker.us-east-1.amazonaws.com` resolves to `10.0.1.96` and a
> public host **times out after 10 s**. Full numbers, commands and the cost
> breakdown are in [RESULTS.md](RESULTS.md).
>
> **Known issues in this comparison.** The two rows differ in hardware class
> (CPU vs GPU) and precision, so the latency and throughput deltas are not
> attributable to the hosting path. They also did not do the same amount of
> work: the SageMaker path ran to its 10-token cap (`finish_reason: "length"`)
> while llama.cpp answered and stopped at ~3 tokens, so the SageMaker latency
> includes unwanted continuation. Equal-work sampling would narrow the
> tokens/sec gap by an amount these numbers cannot settle. A re-deploy to
> match generation and surface per-request token counts is **planned but not
> yet done**. Details: [RESULTS.md § Known issues](RESULTS.md#known-issues-in-the-comparison).

> ### Scope
>
> The CDK constructs are verified against the installed `aws-cdk-lib` (see
> [docs/API-NOTES.md](docs/API-NOTES.md)). The inference host's bootstrap script
> was run on 2026-10-13: llama.cpp builds on AL2023, the Qwen2.5-0.5B model
> downloads, and `/health` returns `{"status":"ok"}`. The SageMaker stage was run
> on 2026-10-18. Six bugs were found and fixed across the two deployments — see
> "What broke and how I found it" below.

## Shape

```
            provider VPC                        consumer VPC
   ┌────────────────────────────┐     ┌──────────────────────────────┐
   │  EC2 inference host        │     │  probe Lambda                │
   │  (llama.cpp, :8080)        │     │        │                     │
   │        ▲                   │     │        ▼                     │
   │        │                   │     │  interface VPC endpoint      │
   │  internal NLB  ────────────┼─────┼──────────┘  (no IGW, no NAT)  │
   │  (:443)                    │     │                              │
   │        ▲                   │     │  sagemaker.runtime endpoint  │
   │  VPC endpoint service      │     │        ▲                     │
   └────────────────────────────┘     │        │ probe Lambda        │
                                      └────────┼─────────────────────┘
   ┌────────────────────────────┐              │
   │  SageMaker (managed)       │              │
   │  endpoint ◄────────────────┼──────────────┘
   │    ▲ Model Registry        │
   │    │ Training Job → S3     │
   └────────────────────────────┘
```

Two VPCs and a managed service, deliberately: the point is that the consumer
reaches both models **without a path to the internet**, and that the same call
from a laptop fails.

## Run

```sh
export AWS_PROFILE=arch        # profiles are local; never committed
cp .env.example .env           # not auto-loaded: `set -a; source .env; set +a`
uv sync                        # install (uv, Python 3.12+)
npm install -g aws-cdk         # the CDK CLI is a Node tool, not a pip package
just verify                    # lint, tests, cdk synth — hermetic, no AWS calls
just deploy                    # all three stacks
                               #   then fill in .env from the stack outputs
just probe --private           # the measured path
just probe --public            # the negative test — expected to fail
just egress                    # bytes that left the inference subnet — expect 0

# SageMaker stage — phase 2, once the stack outputs are in .env
just sagemaker-train --wait    # Training Job: 300 samples, 1 epoch, ml.g5.xlarge
just sagemaker-register        # Model Registry entry from the job's artifact
just sagemaker-deploy          # records the ARN, then adds the endpoint
just sagemaker-spinup          # endpoint creation duration, from CFN events
just sagemaker-probe --private # p50/p95/p99, tokens/sec, $/1k
just sagemaker-probe --public  # this VPC has no internet route — expected to fail
just metrics --minutes 30      # CloudWatch: invocations, latency, 4XX/5XX

just down                      # tear all three stacks down
```

Both interface endpoints bill hourly while they exist, and so does a real-time
SageMaker endpoint, so `just down` is not optional — and the provider stack (NAT
gateway, NLB, EC2) only starts deleting once the consumer finishes. `down` also
clears the model package ARN, so a later `deploy` cannot resurrect the endpoint.

## Status

- [x] scaffold
- [x] inference host serving a model (bootstrap verified: llama.cpp builds on AL2023, Qwen2.5-0.5B-Instruct GGUF downloads, `/health` returns `{"status":"ok"}`)
- [x] internal NLB + VPC endpoint service
- [x] consumer interface endpoint, cross-VPC call working (p50 162.1 ms, 17.7 tok/s)
- [x] public path proven to fail (`ConnectError: Network is unreachable`)
- [x] flow logs showing zero public egress (0 bytes in a 1-min window while serving)
- [x] latency and tokens/sec measured (p50 162.1 ms, 17.7 tok/s, n=20 on 2026-10-13; 147.5 ms, 19.6 tok/s on 2026-10-18)
- [x] SageMaker Training Job completes, artifact in S3, Model Registry entry approved (289 billable seconds, 2026-10-18)
- [x] endpoint deployed **from the registry entry**, serving (342 s provisioning, p50 68.5 ms, 144.4 tok/s)
- [x] `sagemaker.runtime` interface endpoint: private DNS inside the VPC, no internet route from the same VPC
- [x] CloudWatch endpoint metrics (`Invocations`, `ModelLatency`, 4XX/5XX), dashboard and 5XX alarm; `just metrics`
- [x] all three stacks destroyed, zero billable resources left (2026-10-18)

## What broke and how I found it

1. **Zone-id prefix in `vpc_endpoint_dns_entries`.** The Lambda's `ENDPOINT_DNS_NAME`
   came from `Fn.select(0, endpoint.vpc_endpoint_dns_entries)`, which returns
   `zone-id:dns-name`. urllib parsed `zone-id` as the host and the rest as a
   nonnumeric port. Fix: `Fn.select(1, Fn.split(":", …))` strips the prefix. Found
   by the first `just probe --private` returning `InvalidURL: nonnumeric port`.

2. **Port 80 vs 443.** The Lambda and the local probe built URLs as
   `http://dns-name/health` (port 80), but the interface endpoint listens on 443.
   Port 80 traffic was silently dropped by the endpoint SG (which allows 443
   only), producing a 20-second timeout. Fix: `:443` in every URL. Found by the
   probe timing out after the DNS fix.

3. **NLB security group had no inbound rule.** The NLB SG was created with
   `allow_all_outbound=True` but no ingress rule on the service port. Traffic
   from the VPC endpoint service was blocked at the NLB. Fix: allow the VPC CIDR
   on port 443. Found alongside issue 2 — both contributed to the timeout, so
   both were fixed before retesting.

4. **The training job's `results.json` is inside `output.tar.gz`.** Everything
   written to `/opt/ml/output/data` is tarred into
   `<job>/output/output.tar.gz` under the job's output prefix, not copied as
   loose files. The first run completed and then the local runner crashed with
   `NoSuchKey` looking for `…/output/data/results.json`. Found by re-reading the
   job's S3 prefix; the fix reads the archive.

5. **The TGI container refuses to start without `HF_MODEL_ID`.** First endpoint
   deploy failed its ping health check after 22 minutes; the container log said
   `HF_MODEL_ID must be set` on repeat. SageMaker had already unpacked the model
   into `/opt/ml/model`, but the DLC's launcher requires the variable regardless.
   Fixed by putting `HF_MODEL_ID=/opt/ml/model` in the model package's inference
   container, which also keeps the container from fetching weights from the Hub.

6. **transformers 5.x writes a tokenizer config the serving image cannot read.**
   Second deploy failed the same way, with
   `AttributeError: 'list' object has no attribute 'keys'` in
   `tokenization_auto.py`. The training image (transformers 5.3) serialises
   `extra_special_tokens` as a list; the transformers inside TGI 3.3.6 expects a
   mapping. Fixed in the job itself (`training/tokenizer_compat.py`) and re-run,
   rather than hand-patching the artifact — the artifact has to be something the
   command produces.

## Notes

- The repo holds no account IDs, ARNs, or credentials. Local values live in
  `LOCAL.md`, which is gitignored.
- Why three stacks, why the endpoint is deployed from the registry, and why a NAT
  gateway is used at all: [docs/DECISIONS.md](docs/DECISIONS.md).
