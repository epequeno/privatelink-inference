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
