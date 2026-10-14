# privatelink-inference

Most "private" model endpoints are private by convention — a security group, a
private subnet, and hope. This one is private by construction: a self-hosted
inference server behind an **internal NLB** and a **VPC endpoint service**, reached
from a separate VPC through an **interface endpoint**. There is no public route to
it, and the repo proves that rather than asserting it.

> ### Measured 2026-10-13
>
> Private path: p50 **162.1 ms**, **17.7 tokens/sec** (n=20). Public path:
> **`ConnectError: Network is unreachable`**. Egress while serving: **0 bytes**.
> Full numbers and commands in [RESULTS.md](RESULTS.md).

> ### Scope
>
> The CDK constructs are verified against the installed `aws-cdk-lib` (see
> [docs/API-NOTES.md](docs/API-NOTES.md)). The inference host's bootstrap script
> was run on 2026-10-13: llama.cpp builds on AL2023, the Qwen2.5-0.5B model
> downloads, and `/health` returns `{"status":"ok"}`. Three bugs were found and
> fixed during deployment — see "What broke and how I found it" below.

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
   │        ▲                   │     │  no route to the internet    │
   │  VPC endpoint service      │     └──────────────────────────────┘
   └────────────────────────────┘
```

Two VPCs, one app, deliberately: the point is that the consumer reaches the model
**without a path to the internet**, and that the same call from a laptop fails.

## Run

```sh
export AWS_PROFILE=arch        # profiles are local; never committed
cp .env.example .env           # not auto-loaded: `set -a; source .env; set +a`
uv sync                        # install (uv, Python 3.12+)
npm install -g aws-cdk         # the CDK CLI is a Node tool, not a pip package
just verify                    # lint, tests, cdk synth
just deploy                    # provider + consumer stacks
                               #   then fill in .env from the stack outputs
just probe --private           # the measured path
just probe --public            # the negative test — expected to fail
just egress                    # bytes that left the inference subnet — expect 0
just down                      # tear both stacks down
```

The interface endpoint bills hourly while it exists, so `just down` is not optional — and
the provider stack (NAT gateway, NLB, EC2) only starts deleting once the consumer finishes.

## Status

- [x] scaffold
- [x] inference host serving a model (bootstrap verified: llama.cpp builds on AL2023, Qwen2.5-0.5B-Instruct GGUF downloads, `/health` returns `{"status":"ok"}`)
- [x] internal NLB + VPC endpoint service
- [x] consumer interface endpoint, cross-VPC call working (p50 162.1 ms, 17.7 tok/s)
- [x] public path proven to fail (`ConnectError: Network is unreachable`)
- [x] flow logs showing zero public egress (0 bytes in a 1-min window while serving)
- [x] latency and tokens/sec measured (p50 162.1 ms, 17.7 tok/s, n=20)

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

## Notes

- The repo holds no account IDs, ARNs, or credentials. Local values live in
  `LOCAL.md`, which is gitignored.
- Why two stacks and not one, and why a NAT gateway is used at all:
  [docs/DECISIONS.md](docs/DECISIONS.md).
