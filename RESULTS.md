# Results

Every number here is produced by a command in this repo and dated. A row with no
date has not been measured — and a number without its command is not evidence.

## Headline

| Metric | Value | Command | n | Date |
|---|---|---|---|---|
| Private-path p50 latency (ms) | TODO | `just probe --private` | — | — |
| Private-path tokens/sec | TODO | `just probe --private` | — | — |
| Public-path result | TODO — expected: refused | `just probe --public` | — | — |
| Public egress from inference subnet (bytes) | TODO — expected: 0 during inference | flow logs | — | — |
| Cost per hour, both stacks up (USD) | TODO | AWS Cost Explorer | — | — |

## The claim these rows are meant to support

That the inference endpoint is **unreachable** except through the VPC endpoint: the
private path works, the public path fails, and the inference subnet sends nothing
to the internet while serving. The last two rows are the ones that test it —
latency proves availability, not privacy.

## How to reproduce

```sh
export AWS_PROFILE=arch
just deploy
just probe --private     # works
just probe --public      # must fail; capture the error verbatim
just down
```

Record the commit SHA, the date, and the exact command beside every number. The
public-path row is only meaningful if the captured failure is pasted in, not
summarised.
