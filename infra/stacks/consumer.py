"""Consumer stack — reaches the model with no route to the internet.

The subnets are ``PRIVATE_ISOLATED``: no internet gateway, no NAT gateway, no
public IP. So a successful call from here cannot be explained by the consumer
reaching the model over the internet — the only path is the interface endpoint.

The probe runs as a Lambda inside that VPC rather than on a bastion, because a
bastion would need SSM endpoints (three more interface endpoints, more cost, more
moving parts) purely for convenience. ``aws lambda invoke`` is a control-plane
API call, so it works from a laptop without any route into the VPC.
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

PROBE_ASSET = Path(__file__).resolve().parents[1] / "lambda" / "sagemaker_probe"

SERVICE_PORT = 443

# Uses urllib from the standard library: an inline Lambda cannot pip-install.
# After the health check, it makes N completions calls and returns per-sample
# latencies and token counts. The local probe computes p50 and tokens/sec from
# the raw samples — the stats stay on the laptop so they are testable without
# AWS. The prompt is deliberately tiny (max_tokens 10) so N calls fit the
# timeout on a CPU-only 0.5B model.
PROBE_CODE = """
import json
import os
import time
import urllib.error
import urllib.request

DNS_NAME = os.environ["ENDPOINT_DNS_NAME"]
HEALTH_URL = f"http://{DNS_NAME}:443/health"
COMPLETIONS_URL = f"http://{DNS_NAME}:443/v1/chat/completions"

PROMPT = {"messages": [{"role": "user", "content": "Say hello in one word."}], "max_tokens": 10}


def _post_json(url, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(req, timeout=timeout)


def handler(event, context):
    samples = int(event.get("samples", 5))
    started = time.perf_counter()

    # Health first — if the endpoint is unreachable, the completions calls
    # would each time out and waste the whole Lambda window.
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as response:
            body = response.read(2048).decode("utf-8", "replace")
        health = {"ok": True, "status": response.status, "body": body[:200]}
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        return {
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"health: {type(exc).__name__}: {exc}",
        }

    results = []
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            with _post_json(COMPLETIONS_URL, PROMPT) as resp:
                data = json.loads(resp.read())
            elapsed_s = time.perf_counter() - t0
            completion_tokens = data.get("usage", {}).get("completion_tokens", 0)
            tps = completion_tokens / elapsed_s if elapsed_s > 0 else 0.0
            results.append({
                "elapsed_ms": round(elapsed_s * 1000, 1),
                "completion_tokens": completion_tokens,
                "tokens_per_sec": round(tps, 1),
            })
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": f"completions: {type(exc).__name__}: {exc}",
            }

    return {
        "ok": True,
        "health": health,
        "samples": results,
        "total_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }
"""


class ConsumerStack(cdk.Stack):
    """VPC B: interface endpoint and the probe function."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        service_name: str,
        sagemaker_endpoint_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = ec2.Vpc(
            self,
            "ConsumerVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                )
            ],
        )

        endpoint_sg = ec2.SecurityGroup(
            self,
            "EndpointSg",
            vpc=vpc,
            description="Interface endpoint: HTTPS to the endpoint service",
            allow_all_outbound=False,
        )
        endpoint_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(SERVICE_PORT),
            "from the consumer VPC",
        )

        # service_name comes from the provider stack output. Importing the value
        # rather than passing the object keeps this the same shape a cross-account
        # consumer would need.
        endpoint = ec2.InterfaceVpcEndpoint(
            self,
            "InferenceEndpoint",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointService(service_name, SERVICE_PORT),
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[endpoint_sg],
            private_dns_enabled=False,
        )

        probe_fn = lambda_.Function(
            self,
            "ProbeFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_inline(PROBE_CODE),
            timeout=Duration.seconds(60),  # N completions calls, not just one health check
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            environment={
                # vpc_endpoint_dns_entries are "zone-id:dns-name"; strip the
                # zone-id prefix so urllib doesn't read it as a port number.
                "ENDPOINT_DNS_NAME": cdk.Fn.select(
                    1, cdk.Fn.split(":", cdk.Fn.select(0, endpoint.vpc_endpoint_dns_entries))
                )
            },
        )

        # The same "no public route" constraint, applied to a managed AWS service.
        # `sagemaker.runtime` is the data-plane API (InvokeEndpoint); with private
        # DNS enabled the SDK's normal hostname resolves to these ENIs, so the
        # call never leaves the VPC.
        sagemaker_sg = ec2.SecurityGroup(
            self,
            "SageMakerEndpointSg",
            vpc=vpc,
            description="sagemaker.runtime interface endpoint: HTTPS from the VPC",
            allow_all_outbound=False,
        )
        sagemaker_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(SERVICE_PORT),
            "from the consumer VPC",
        )
        sagemaker_endpoint = ec2.InterfaceVpcEndpoint(
            self,
            "SageMakerRuntimeEndpoint",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[sagemaker_sg],
            private_dns_enabled=True,  # the SDK is used unmodified; DNS does the routing
        )

        # boto3 and botocore ship in the Lambda runtime, so this asset needs no
        # bundling step and `just verify` never invokes it.
        sagemaker_probe_role = iam.Role(
            self,
            "SageMakerProbeRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        sagemaker_probe_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sagemaker:InvokeEndpoint"],
                resources=[
                    f"arn:aws:sagemaker:{self.region}:{self.account}:endpoint/"
                    f"{sagemaker_endpoint_name}"
                ],
            )
        )
        # Attached before the function exists: Lambda creates the ENIs at creation
        # time, so the role needs these permissions by then, not by first invoke.
        sagemaker_probe_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:CreateNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DeleteNetworkInterface",
                ],
                resources=["*"],
            )
        )
        sagemaker_probe = lambda_.Function(
            self,
            "SageMakerProbeFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_asset(str(PROBE_ASSET)),
            role=sagemaker_probe_role,
            timeout=Duration.seconds(120),
            memory_size=256,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            # AWS_REGION is set by the Lambda runtime and cannot be overridden.
            environment={"SAGEMAKER_ENDPOINT_NAME": sagemaker_endpoint_name},
        )
        CfnOutput(self, "EndpointId", value=endpoint.vpc_endpoint_id)
        CfnOutput(self, "ProbeFunctionName", value=probe_fn.function_name)
        CfnOutput(self, "SageMakerProbeFunctionName", value=sagemaker_probe.function_name)
        CfnOutput(self, "SageMakerEndpointId", value=sagemaker_endpoint.vpc_endpoint_id)
        CfnOutput(
            self,
            "EndpointDnsName",
            # Same strip — the public probe needs a bare DNS name, not zone-id:dns.
            value=cdk.Fn.select(
                1, cdk.Fn.split(":", cdk.Fn.select(0, endpoint.vpc_endpoint_dns_entries))
            ),
        )
        CfnOutput(
            self,
            "EndpointDnsEntries",
            value=cdk.Fn.join(",", endpoint.vpc_endpoint_dns_entries),
        )
