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
