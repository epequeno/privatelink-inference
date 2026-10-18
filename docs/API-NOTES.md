# API notes — what is verified, and what is still an assumption

**Status: verified by deployment on 2026-10-13.** The first table was read out of
the installed `aws-cdk-lib` 2.240.0 on this machine. The second table was checked
during the first deployment — most rows are now confirmed and moved up. Only
cost remains unmeasured.

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
| `ec2.FlowLog` props: `resource_type`, `destination`, `traffic_type`, `max_aggregation_interval`, `log_format`; `FlowLogResourceType.from_vpc`, `FlowLogDestination.to_cloud_watch_logs`, `FlowLogMaxAggregationInterval.ONE_MINUTE`, `FlowLogTrafficType.ALL` | class signature |
| `ec2.LogFormat` fields `SRC_ADDR`, `DST_ADDR`, `BYTES`, `ACTION`, `LOG_STATUS`, `START_TIMESTAMP`; custom `log_format` renders as `${srcaddr} ${dstaddr} ${bytes} ${action} ${log-status} ${start}` | synth output + class members |
| `vpc_endpoint_dns_entries` returns `zone-id:dns-name` — `Fn.split(":", …)` strips the prefix | deployment + probe failure |
| The bootstrap builds llama.cpp on AL2023 with `-DGGML_NATIVE=ON` (no CUDA flags needed), downloads Qwen2.5-0.5B-Instruct q4_k_m (468 MB), and starts `inference.service` | deployment: `systemctl is-active` → `active`, `/health` → `{"status":"ok"}` |
| llama.cpp exposes `/health` (returns `{"status":"ok"}`) and `/v1/chat/completions` (returns `usage.completion_tokens`) | deployment probe |
| Plain TCP on 443 fronted by the endpoint service works end-to-end; the NLB SG needs an inbound rule for the VPC CIDR on 443 | deployment: probe succeeded after adding the rule |
| `private_dns_enabled=False` works — the endpoint's own DNS name resolves from the consumer VPC | deployment: Lambda reached the endpoint |
| The probe Lambda's urllib call works from `PRIVATE_ISOLATED` through the interface endpoint | deployment: 20/20 completions succeeded |
| Flow logs publish to CloudWatch and `just egress` reads them — 0 bytes while serving (1-min window) | deployment: egress report |
| `aws_sagemaker.CfnModel` takes `containers=[ContainerDefinitionProperty(model_package_name=…)]`; deploying from a registry entry needs no `image` or `model_data_url` | class signature + synth |
| `CfnEndpointConfig.ProductionVariantProperty` has `container_startup_health_check_timeout_in_seconds` and `model_data_download_timeout_in_seconds` | class signature |
| `ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME` renders as `com.amazonaws.<region>.sagemaker.runtime` | synth output |
| `AWS_REGION` cannot be set on a Lambda's `environment` — the runtime reserves it (CDK refuses at synth time) | synth error |
| SageMaker DLC images live in account `763104351884`; `huggingface-pytorch-training:2.9.0-transformers5.3.0-gpu-py312-cu130-ubuntu22.04` and `huggingface-pytorch-tgi-inference:2.7.0-tgi3.3.6-gpu-py311-cu124-ubuntu22.04-v1.2` both resolve in us-east-1 | `ecr describe-images`, 2026-10-18 |
| Service quotas: `ml.g5.xlarge` for training usage = 1, for endpoint usage = 2 — enough for a one-instance job and a one-instance endpoint without a quota request | `service-quotas list-service-quotas`, 2026-10-18 |
| `CreateModelPackage` with an `InferenceSpecification` decouples the serving image from the training image, which is what lets a job trained in the HF training container be served by TGI | call shape verified against the API model; **deployment evidence pending** — see RESULTS.md |
| `CreateModelPackage` rejects `Tags` on a package version ("Tags are not supported in Model Package versions") and rejects `Framework: "TGI"` (enum: TENSORFLOW, PYTORCH, …) | two `ValidationException`s from the call, 2026-10-18 |
| A script-mode job's `/opt/ml/output/data` arrives as `<job>/output/output.tar.gz`, not as loose objects | first job: `NoSuchKey` for `…/output/data/results.json` |
| The `huggingface-pytorch-tgi-inference` DLC exits with `HF_MODEL_ID must be set` even when SageMaker has unpacked the artifact into `/opt/ml/model` | failed endpoint health check, container log |
| transformers 5.x serialises `extra_special_tokens` as a list; the transformers inside TGI 3.3.6 calls `.keys()` on it and the container cannot load the tokenizer | failed endpoint health check, `AttributeError` in `tokenization_auto.py` |

## Unverified — check these before trusting a deployment

| Assumption | Why it is risky |
|---|---|
| Cost per hour with the stacks up | Never estimated. An idle NLB, two interface endpoints, a NAT gateway, a running EC2 instance and a SageMaker endpoint all bill continuously |

## How to close this file out

Work the second table one row at a time, replacing each with the evidence that
confirms it, the way the previous project's spec notes did. Until a row moves up,
treat the corresponding part of the system as unbuilt.
