# Results

Every number here is produced by a command in this repo and dated. A row with no
date has not been measured — and a number without its command is not evidence.

## Headline — llama.cpp on EC2 (first measured 2026-10-13, re-measured 2026-10-18)

| Metric | Value | Command | n | Date |
|---|---|---|---|---|
| Private-path p50 latency (ms) | 162.1 (10-13) · **147.5** (10-18) | `just probe --private --samples 20` | 20 | 2026-10-18 |
| Private-path tokens/sec | 17.7 (10-13) · **19.6** (10-18) | `just probe --private --samples 20` | 20 | 2026-10-18 |
| Public-path result | `ConnectTimeout` after 17194 ms (10-13: `ConnectError: [Errno 51] Network is unreachable` after 17238 ms) | `just probe --public` | 1 | 2026-10-18 |
| Public egress from inference subnet (bytes) | 0 (1-min window while serving; 1.5 MB during bootstrap, expected) | `just egress --minutes 1` | 1 | 2026-10-13 |

The public-path error text changed between runs because it depends on the
*client's* network, not on the endpoint: a laptop that blackholes the packet
reports a timeout, one that has no route reports `Network is unreachable`. Same
conclusion either way — which is why the failure is captured verbatim rather than
summarised.

## Hosting comparison — one model, two hosting paths, same day

Both rows serve **Qwen2.5-0.5B-Instruct**. They are not the same weights: the
EC2 row serves the base model at q4_k_m over llama.cpp on a **CPU** instance, and
the SageMaker row serves the same base model with the LoRA adapter from this
repo's training job merged in, at bf16 over TGI on a **GPU**. The comparison is
therefore about the *hosting path* — provisioning, latency, throughput, cost — not
about model quality, and the numbers should be read that way.

| | llama.cpp on EC2 | SageMaker real-time endpoint |
|---|---|---|
| Instance | `c6i.large` (2 vCPU) | `ml.g5.xlarge` (1× A10G) |
| p50 latency | **147.5 ms** | **68.5 ms** |
| p95 latency | not measured | **73.3 ms** |
| p99 latency | not measured | **73.9 ms** |
| Tokens/sec (single stream) | **19.6** | **144.4** |
| Provisioning time | bootstrap on first boot (llama.cpp build + model fetch, minutes) | **342 s** provisioning → `InService` |
| Cost while available | ~$0.13/h (EC2 $0.085 + internal NLB $0.0225 + interface endpoint $0.02) | **$1.41/h** |
| $ per 1k requests | **$0.0052** | **$0.0268** |
| n | 20 | 20 |
| Date | 2026-10-18 | 2026-10-18 |
| Command | `just probe --private --samples 20` | `just sagemaker-probe --private --samples 20` |

`$ per 1k` assumes **100% single-stream utilisation** — `rate × 1000 × p50/3600`.
That is the best case for a per-hour resource and the only figure derivable from a
latency measurement alone; under real concurrency the SageMaker figure falls
proportionally with batching, and the EC2 one barely moves because a 2-vCPU host
is already saturated at one stream. Endpoint spin-up is measured from
CloudFormation events (`Endpoint` `CREATE_IN_PROGRESS` → `CREATE_COMPLETE`).

## Known issues in the comparison

Neither is fixed. Both are arithmetic on the numbers above plus one piece of evidence that
was not surfaced, and both are settled by the re-deploy that is **planned but not yet done**.

### The two paths did not do the same amount of work

Same prompt, same 10-token cap on both — but the stored probe output
(`results/sagemaker_probe_20261018-224054.json`, gitignored) shows **all 20 samples hitting the
cap**, `finish_reason: "length"`, exactly 10 tokens each:

```
"generated_text": " Hello. \n\nThis is a very simple sentence."
"details": {"finish_reason": "length", "generated_tokens": 10}
```

The model answered in two tokens and then continued for eight more to fill the budget. The
llama.cpp path, called through `/v1/chat/completions`, applies a chat template and stops:
19.6 tok/s × 0.1475 s = **2.9 tokens**. So the SageMaker latency includes roughly seven tokens
of unwanted continuation that the llama.cpp call never generated, and the two paths were not
answering the same question.

**This does not make the 7.4× tokens/sec ratio a token-count artifact.** Both figures are
genuine rates — tokens per second — and a rate does not scale with the size of the sample. What
it does mean is that the two rates are measured over very different sample lengths: a 2.9-token
request is dominated by prefill and fixed overhead, so llama.cpp's rate is dragged *below* its
steady-state generation rate, while the 10-token SageMaker rate sits closer to steady state.
Equal-work sampling would narrow the gap, but by how much **cannot be derived from the numbers
here** — it needs the prefill/decode split, which neither probe records. That is what the
re-deploy would settle.

`completion_tokens` is recorded per sample by both probes — the SageMaker handler comments
that it is kept "so a reader can check what was actually counted" — but it is not surfaced in
the table above. Surfacing it is the change that would have caught this, and it is part of the
re-deploy.

### Not apples-to-apples on hardware

The two rows differ in four things at once: **CPU** (`c6i.large`) vs **GPU** (`ml.g5.xlarge`),
q4_k_m vs bf16, llama.cpp vs TGI, and base weights vs base + adapter. The weights difference is
stated above. The rest mean the latency and throughput deltas are dominated by **CPU vs GPU**,
so they are not attributable to the *hosting path* either. Only two rows are attributable to
that choice — provisioning time and `$/1k` — and both are sound.

Phase 1 of the plan chose Qwen2.5-0.5B precisely so this comparison would be apples-to-apples.
The same base model was used, but not the same hardware class or precision, so that goal was
not met.

## Model development — a real SageMaker Training Job

| Item | Value |
|---|---|
| Command | `just sagemaker-train --wait` then `just sagemaker-register` |
| Job | `qwen-legal-lora-20261018-221855` (a first run, `…-212213`, completed too) |
| Instance | `ml.g5.xlarge` |
| Billable seconds (job 2) | **289 s** (job 1: 290 s) |
| Data | 300 train / 50 validation rows, 1 epoch (`data/sft/`) |
| Model | Qwen2.5-0.5B-Instruct, LoRA r=16 α=32, bf16 |
| Trainable params | 8,798,208 / 502,830,976 (**1.75%**) |
| Train / eval loss | 2.485 / **2.3875** |
| Artifact | `model.tar.gz` (787 MB) in the artifacts bucket, registry entry `qwen-legal-lora/3`, approved |
| Date | 2026-10-18 |

The point of the job is the AWS surface and the discipline, not model quality:
300 rows and one epoch produce a model whose loss barely moves. The endpoint in
the comparison table is deployed **from the registry entry**, so the chain is
training job → registered model → endpoint, not three unrelated facts.

## The privacy claim, extended to a managed service

The consumer VPC is `PRIVATE_ISOLATED` — no IGW, no NAT, no public IP — and now
reaches SageMaker through the `sagemaker.runtime` interface endpoint.

| Probe | Result | Command | Date |
|---|---|---|---|
| DNS for `runtime.sagemaker.us-east-1.amazonaws.com` inside the VPC | `10.0.1.96` (RFC1918 — private DNS is doing the routing) | `just sagemaker-probe --public` | 2026-10-18 |
| Reachability of a public host from the same VPC | **`TimeoutError: timed out` after 10010 ms** | `just sagemaker-probe --public` | 2026-10-18 |
| `InvokeEndpoint` from the same VPC | 21/21 succeeded (1 warm-up + 20 timed) | `just sagemaker-probe --private --samples 20` | 2026-10-18 |

Success on the third row with failure on the second is the evidence: the invoke
did not travel over the internet. The error text is a **timeout**, not
`Network is unreachable` as in the EC2 row — the isolated VPC drops the packet
rather than failing the route lookup. That is a weaker signal on its own, which
is why the DNS row is recorded beside it.

## Monitoring

`just metrics --minutes 30`, 2026-10-18, after the 20-sample probe:

```
Invocations (Sum)            datapoints=9  sum=27.0 last=21.0
ModelLatency (Average)       datapoints=6  sum=573332.0 last=59709.0   (µs)
OverheadLatency (Average)    datapoints=6  sum=253280.0 last=37199.0   (µs)
Invocation5XXErrors (Sum)    datapoints=7  sum=0.0
Invocation4XXErrors (Sum)    datapoints=6  sum=0.0
```

CloudWatch's `ModelLatency` (59.7 ms average, from the service side) is lower
than the probe's p50 (68.5 ms) because the probe also pays API overhead — the
same split the metrics show as `OverheadLatency`.

## Cost

**Not yet attributable from Cost Explorer.** The spend is real and is estimated
below from billed duration × list price; `aws ce get-cost-and-usage` for
2026-10-18 showed **no SageMaker line** at the time of teardown (billing data
lags), and the tag filter on `project=privatelink-inference` returned `$0`
because cost-allocation-tag activation is denied on a linked account. This is the
one row in this file that is an estimate rather than a measurement, and it says so.

| Line | Duration | Rate | Cost |
|---|---|---|---|
| Training job 1 (failed artifact, still billable) | 290 s | $1.41/h | $0.114 |
| Training job 2 (used for the endpoint) | 289 s | $1.41/h | $0.113 |
| Endpoint, two failed deploys (health check ran ~22 min each) | ~44 min | $1.41/h | ~$1.03 |
| Endpoint, serving window | ~18 min | $1.41/h | ~$0.42 |
| Provider + consumer stacks (EC2, NLB, NAT, 2 interface endpoints) | ~3.5 h | ~$0.17/h | ~$0.60 |
| S3 artifacts (1.6 GB) | — | — | ~$0.04 |
| **Estimated total** | | | **~$2.3** |

The two failed deploys cost more than the measurement. That is the useful number:
on a managed service the expensive failures are the ones that sit in a health
check, not the ones that error out.

## Not measured

- **Serverless inference cold start** — deliberately not deployed; see
  `docs/DECISIONS.md`. Real-time has no per-request cold start to report.
- **Behaviour under concurrency** — every number here is a single stream.
- **Model quality** — eval loss only. No downstream task evaluation was run.
- **llama.cpp p95/p99** — the original probe records only p50; the SageMaker
  probe reports p50/p95/p99.

## How to reproduce

```sh
export AWS_PROFILE=arch
set -a; source .env; set +a
just verify
just deploy                     # three stacks; fill .env from the outputs
just sagemaker-train --wait     # ~7 min, ~$0.11
just sagemaker-register
just sagemaker-deploy           # endpoint, ~6 min
just sagemaker-spinup
just sagemaker-probe --private --samples 20
just sagemaker-probe --public   # must fail
just metrics --minutes 30
just probe --private --samples 20
just down                       # empties the registry, destroys all three
```

Record the commit SHA, the date, and the exact command beside every number. The
negative-test rows are only meaningful if the captured failure is pasted in, not
summarised.
