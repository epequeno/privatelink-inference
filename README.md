# privatelink-inference

Most "private" model endpoints are private by convention — a security group, a
private subnet, and hope. This one is private by construction: a self-hosted
inference server behind an **internal NLB** and a **VPC endpoint service**, reached
from a separate VPC through an **interface endpoint**. There is no public route to
it, and the repo proves that rather than asserting it.

> ### TODO — not yet measured
>
> **No numbers exist yet.** This repo aims to report: private-path p50 latency and
> tokens/sec, the **captured failure** of the same call from outside the VPC, and
> public egress from the inference subnet measured at zero. Record them in
> [RESULTS.md](RESULTS.md). Until a row exists there, this README claims nothing.

> ### Scope — read before trusting anything here
>
> The CDK constructs are verified against the installed `aws-cdk-lib` (see
> [docs/API-NOTES.md](docs/API-NOTES.md), which separates what was read out of the
> SDK from what is still an assumption). **The inference host's bootstrap script has
> never been run** — no deployment has happened, so the model serving path is the
> least trustworthy part of this repo.

## Shape

```
            provider VPC                        consumer VPC
   ┌────────────────────────────┐     ┌──────────────────────────────┐
   │  EC2 inference host        │     │  probe host                  │
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
uv sync                        # install (uv, Python 3.12+)
npm install -g aws-cdk         # the CDK CLI is a Node tool, not a pip package
just verify                    # lint, tests, cdk synth
just deploy                    # provider + consumer stacks
just probe --private           # the measured path
just probe --public            # the negative test — expected to fail
just down                      # tear both stacks down
export AWS_PROFILE=arch        # profiles are local; never committed
```

## Status

- [x] scaffold
- [ ] inference host serving a model (bootstrap script unverified)
- [ ] internal NLB + VPC endpoint service
- [ ] consumer interface endpoint, cross-VPC call working
- [ ] public path proven to fail, captured
- [ ] flow logs showing zero public egress from the inference subnet
- [ ] latency and tokens/sec measured

## What broke and how I found it

*Populate as it happens. This is the section a reviewer actually reads.*

## Notes

- The repo holds no account IDs, ARNs, or credentials. Local values live in
  `LOCAL.md`, which is gitignored.
- Why two stacks and not one, and why a NAT gateway is used at all:
  [docs/DECISIONS.md](docs/DECISIONS.md).
