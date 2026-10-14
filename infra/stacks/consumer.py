"""Consumer stack — reaches the model with no route to the internet.

The subnets are ``PRIVATE_ISOLATED``: no internet gateway, no NAT gateway, no
public IP. So a successful call from here cannot be explained by the consumer
reaching the model over the internet — the only path is the interface endpoint.

The probe runs as a Lambda inside that VPC rather than on a bastion, because a
bastion would need SSM endpoints (three more interface endpoints, more cost, more
moving parts) purely for convenience. ``aws lambda invoke`` is a control-plane
API call, so it works from a laptop without any route into the VPC.
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

SERVICE_PORT = 443

# Uses urllib from the standard library: an inline Lambda cannot pip-install.
PROBE_CODE = """
import json
import os
import time
import urllib.error
import urllib.request

DNS_NAME = os.environ["ENDPOINT_DNS_NAME"]
URL = f"http://{DNS_NAME}/health"


def handler(event, context):
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(URL, timeout=10) as response:
            body = response.read(2048).decode("utf-8", "replace")
        return {
            "ok": True,
            "status": response.status,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "body": body[:200],
        }
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        return {
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {exc}",
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
            timeout=Duration.seconds(30),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            environment={"ENDPOINT_DNS_NAME": cdk.Fn.select(0, endpoint.vpc_endpoint_dns_entries)},
        )

        CfnOutput(self, "EndpointId", value=endpoint.vpc_endpoint_id)
        CfnOutput(self, "ProbeFunctionName", value=probe_fn.function_name)
        CfnOutput(
            self,
            "EndpointDnsEntries",
            value=cdk.Fn.join(",", endpoint.vpc_endpoint_dns_entries),
        )
