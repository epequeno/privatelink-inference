# Decisions

Short log of choices that would otherwise be re-litigated. Newest last.

## 2026-10-13 · Two stacks, not one

The claim is about the boundary between a provider and a consumer. One stack could
not demonstrate that the consumer has no other route, and it would not resemble the
cross-account shape this pattern is normally used for. The consumer imports the
service **name** rather than the rendered object, which is the same wiring a
cross-account consumer would need.

## 2026-10-13 · The consumer subnets are `PRIVATE_ISOLATED`

No internet gateway, no NAT, no public IP. That is what makes a successful call
meaningful: it cannot be explained by the consumer reaching the model over the
internet. Had the consumer used `PRIVATE_WITH_EGRESS`, the result would be
ambiguous and the whole exercise pointless.

## 2026-10-13 · The provider keeps a NAT gateway, for bootstrap only

The honest tension in this repo. The inference host needs egress once — to install
a toolchain and fetch the model. Options were: a NAT gateway (simple, bills hourly),
interface endpoints for S3/SSM (no general egress, more setup), or a baked AMI (no
egress, slowest to iterate). NAT wins while the bootstrap is still changing shape.
**If the bootstrap settles, removing the NAT is the correct move**, and the
`RESULTS.md` egress row is what proves it is safe.

## 2026-10-13 · The probe is a Lambda, not a bastion

A bastion in an isolated subnet needs three more interface endpoints (ssm,
ssmmessages, ec2messages) purely so a human can log in. `aws lambda invoke` is a
control-plane call, so the probe runs from a laptop without any route into the VPC.
Fewer moving parts, less cost, and the probe becomes scriptable.

## 2026-10-13 · `acceptance_required=False`

Same account, so the endpoint attaches without a manual approval step. A real
cross-account provider would set `allowed_principals` instead and leave acceptance
required — worth saying because the current setting is a convenience, not the
recommended production posture.

## 2026-10-13 · The public probe is expected to fail, and that is the result

`just probe --public` exits 0 when the connection is refused and 1 when it
succeeds. The failure is the deliverable; a success means the privacy claim is
false and should block any write-up.

## 2026-10-13 · Egress is measured from flow logs, not asserted

The provider VPC now publishes flow logs to CloudWatch Logs (ONE_MINUTE
aggregation, ALL traffic), and `just egress` sums the bytes whose source is in an
inference subnet and whose destination is outside the VPC and outside link-local.
That definition is what makes "0 bytes" meaningful: intra-VPC traffic, the NLB,
and the VPC DNS resolver (169.254.169.253) are all excluded, so a non-zero number
is genuinely internet-bound. S3 + Athena was the alternative for the report; it
was rejected as more moving parts for a window that fits in one paginated
`filter_log_events` call. The custom `log_format` keeps records short and fixes
the field order the parser reads.

## 2026-10-13 · `egress` exit polarity is inverted from `probe --public`

`just egress` exits 0 when egress is zero (privacy claim holds) and 1 when bytes
leaked. `just probe --public` exits 0 when the call *fails* (claim holds). Both
exit 0 on success of the privacy claim, but the natural reading differs — egress
is a "0 is good" metric, the public probe is a "failure is good" test. Worth
saying because a reader who memorised the public probe's polarity will guess the
egress one wrong.

## 2026-10-13 · The stats stay on the laptop, not in the Lambda

The probe Lambda makes N completions calls and returns the raw per-sample
timings; `probe.py` computes p50 latency and mean tokens/sec locally. The
alternative was computing the summary inside the Lambda and returning only the
two numbers. Keeping raw samples on the wire means the stats logic is unit-test
able without AWS, and a reviewer can re-derive or re-plot from the same data.
The cost is a slightly larger Lambda response — negligible for N ≤ 20.

## 2026-10-13 · `vpc_endpoint_dns_entries` need a zone-id strip

The DNS entries returned by `InterfaceVpcEndpoint.vpc_endpoint_dns_entries` are
`zone-id:dns-name`, not bare DNS names. Passing one directly to urllib makes it
parse the zone-id as the host and the rest as a nonnumeric port. The fix is
`Fn.select(1, Fn.split(":", …))` in the stack, so the Lambda and the local probe
both get a clean DNS name. A `CfnOutput` (`EndpointDnsName`) provides the same
stripped value for the public probe.

## 2026-10-13 · Port 443 in every URL, and the NLB SG inbound rule

Two issues surfaced as one timeout. First, the Lambda and probe used `http://`
(port 80) but the interface endpoint listens on 443 — port 80 was silently
dropped by the endpoint SG. Second, the NLB security group had no inbound rule,
so even with the right port the endpoint service's traffic was blocked at the
NLB. Both were fixed before retesting: `:443` in every URL, and the NLB SG allows
the VPC CIDR on the service port.

## 2026-10-18 · SageMaker is a third stack, not a third VPC

The managed service has no provider VPC of ours to build: SageMaker owns the
hosting account. So the stack contains only what we control — the artifact
bucket, the two execution roles, the Model Registry package group, and the
endpoint — and the "no public route" claim moves to the consumer side, where the
`sagemaker.runtime` interface endpoint lives. Building a fake provider VPC around
a managed service would have looked symmetric and meant nothing.

## 2026-10-18 · The endpoint is deployed from the registry, not from an S3 URL

`CfnModel` takes `ModelPackageName` rather than `ModelDataUrl`, so the chain
training job → model package → endpoint is real: the endpoint cannot be built
from an artifact that was never registered. The registry entry's
`InferenceSpecification` names the **TGI** image while the job trained in the HF
training image — without that split, deploying from the registry would try to
serve with the training container.

## 2026-10-18 · Two-phase deploy through `infra/cdk.context.json`

An endpoint cannot exist before a model package does, and CDK cannot know a
bundle-time ARN at synth time. The stack therefore reads an optional
`model_package_arn` context: absent, it stops at bucket + roles + registry;
present, it adds model, endpoint config, endpoint, dashboard and alarm.
`just sagemaker-deploy` writes that ARN into `infra/cdk.context.json`, which is
**gitignored because the ARN contains the account id**, and `just down` clears it
so a later `deploy` cannot resurrect a destroyed endpoint. `just verify` passes a
placeholder ARN on the command line, so the endpoint constructs are still part of
the offline gate.

## 2026-10-18 · The endpoint's name is fixed, not CDK-generated

`qwen-legal-lora` is a constant. The probe Lambda is configured with the name at
deploy time, so a generated name would couple the two stacks' deploy order and
make the Lambda's IAM resource ARN unsynthesizable before the endpoint exists.

## 2026-10-18 · The negative test had to change shape

For llama.cpp the public probe is "the same URL fails from a laptop". For a
managed AWS service that test is meaningless: the invoke API is public by design,
and a laptop can always call it with credentials. The claim being made is about
the *consumer VPC*, so the probe Lambda reports three things instead — where
`runtime.sagemaker.<region>.amazonaws.com` resolves (must be RFC1918, proving
private DNS), whether that VPC can reach any public host (it cannot, verbatim
error), and N timed invokes that succeed. Success on the third with failure on
the second is the evidence. `just sagemaker-probe --public` exits 0 when the
internet is unreachable, the same polarity as `just egress`.

## 2026-10-18 · Real-time endpoint, with the cost stated rather than hidden

Serverless would scale to zero and be cheaper to leave running, but cold starts
make p50 a measure of start-up rather than of serving, which is the comparison
this repo is built to make. Real-time on `ml.g5.xlarge` costs $1.41/h whether or
not it is called; the control is same-day teardown (`just down`) plus a $50 AWS
Budget on this project's tag, not an estimate in a document. The missing cold
start is recorded as a gap instead of being papered over with a serverless row
that would not be comparable.

## 2026-10-18 · 350 committed samples instead of a pointer to legal-kit

The job's input is committed (`data/sft/`), not read from `~/code/legal-kit`, so
the training command in RESULTS.md reproduces from a fresh clone. The sampler is
committed too, and the README says the sample is small and the point is the AWS
surface. One epoch over 300 rows will not produce a good model; claiming
otherwise would be the more expensive mistake.
