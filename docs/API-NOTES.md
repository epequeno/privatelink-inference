# API notes — what is verified, and what is still an assumption

**Status: partially verified.** The first table was read out of the installed
`aws-cdk-lib` 2.240.0 on this machine, so it is reliable. The second table has not
been checked at all and includes the parts most likely to need iteration.

Two previous projects in this portfolio paid for skipping this step: one was
scaffolded from a release-candidate blog post and later needed ten-plus
corrections, and another claimed an SDK had no `gateway` module when the claim was
only true of an older version. Assumptions about cloud APIs are expensive.

## Verified locally

| Fact | Evidence |
|---|---|
| `ec2.VpcEndpointService` props: `vpc_endpoint_service_load_balancers`, `acceptance_required`, `allowed_principals`, `contributor_insights` | class signature |
| `VpcEndpointService.vpc_endpoint_service_name` — the value the consumer needs | class property |
| `ec2.InterfaceVpcEndpoint` props: `service`, `vpc`, `subnets`, `private_dns_enabled`, `security_groups`, `lookup_supported_azs` | class signature |
| `InterfaceVpcEndpoint.vpc_endpoint_id`, `.vpc_endpoint_dns_entries` | class properties |
| `ec2.InterfaceVpcEndpointService(name, port)` — how a consumer names a service it did not create | class signature |
| `elbv2.NetworkLoadBalancer` props: `vpc`, `internet_facing`, `cross_zone_enabled`, `vpc_subnets` | class signature |
| `elbv2.NetworkTargetGroup` props: `port`, `protocol`, `targets`, `health_check`, `target_type`, `vpc` | class signature |
| `NetworkListener` exists and has `add_targets`; `Protocol.TCP` exists | class members |
| `ec2.Instance` props: `instance_type`, `machine_image`, `vpc`, `vpc_subnets`, `security_group`, `role`, `user_data`, `block_devices` | class signature |
| `Instance.instance_private_dns_name`, `.instance_id` | class properties |
| `ec2.Vpc` props: `max_azs`, `nat_gateways`, `subnet_configuration`, `ip_addresses`, `restrict_default_security_group` | class signature |
| `PRIVATE_ISOLATED` is a subnet type — no internet gateway, no NAT | `SubnetType` enum |

## Unverified — check these before trusting a deployment

| Assumption | Why it is risky |
|---|---|
| **The host bootstrap script** (`BOOTSTRAP` in `provider.py`) | Never executed. It builds llama.cpp from source, which may take many minutes, may fail on AL2023 (toolchain versions, cmake flags), and may need `-DGGML_CUDA=OFF` style adjustments |
| The GGUF URL and filename for Qwen2.5-0.5B-Instruct | Copied from a conventional naming scheme, not fetched. A 404 means the host serves nothing |
| llama.cpp's `/health` path and port | Assumed; the real server exposes `/health` and `/v1/chat/completions`, but confirm against the version built |
| NLB health-check timing | TCP checks every 30 s with 2 healthy thresholds, so the target may sit unhealthy for ~60 s after boot — the first probe can fail for that reason and it is not a failure of the design |
| Whether a TCP listener on 443 is right, versus a TLS listener | Plain TCP on 443 is what is configured. A TLS listener would need a certificate and changes what the endpoint service must front |
| `private_dns_enabled=False` is acceptable | With it off, callers use the endpoint's own DNS name. Turning it on requires the service provider to enable private DNS, which is an extra step |
| Whether an interface endpoint in an isolated subnet needs the endpoint SG's inbound rule as configured | The rule allows the VPC CIDR on 443; the service must also allow the endpoint |
| Cost per hour with both stacks up | Never estimated. An idle NLB, an interface endpoint, and a running EC2 instance all bill continuously |
| Whether the probe Lambda's urllib call works from `PRIVATE_ISOLATED` | It should — the endpoint is in the same VPC — but the Lambda needs its ENI and the endpoint SG must permit it |

## How to close this file out

Work the second table one row at a time, replacing each with the evidence that
confirms it, the way the previous project's spec notes did. Until a row moves up,
treat the corresponding part of the system as unbuilt.
