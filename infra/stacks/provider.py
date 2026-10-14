"""Provider stack — a self-hosted inference server that has no public route to it.

Contents: a VPC, an EC2 inference host, an **internal** NLB, and a VPC endpoint
service (PrivateLink) fronting that NLB. The consumer VPC in the sibling stack
reaches the model only through the endpoint service.

Every construct used here was verified against the installed aws-cdk-lib rather
than taken from documentation — see docs/API-NOTES.md for the list and for what is
still unverified (in particular, the host bootstrap script, which has never run).

The NAT gateway exists for bootstrap only (installing the runtime, fetching the
model). The inference path itself does not leave the VPC, and proving that is the
point of the project — see docs/DECISIONS.md.
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_targets as elbv2_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

# Small, CPU-runnable, and served over llama.cpp's OpenAI-compatible endpoint.
MODEL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF"
    "/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"
)
INFERENCE_PORT = 8080
SERVICE_PORT = 443
HEALTH_CHECK_PATH = "/health"

# UNVERIFIED — this bootstrap has never been executed. It builds llama.cpp from
# source and fetches the model over the NAT gateway. Expect to iterate on it.
BOOTSTRAP = f"""#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/privatelink-bootstrap.log) 2>&1

dnf install -y git gcc-c++ cmake make

git clone --depth 1 https://github.com/ggml-org/llama.cpp /opt/llama.cpp
cmake -S /opt/llama.cpp -B /opt/llama.cpp/build -DGGML_NATIVE=ON
cmake --build /opt/llama.cpp/build --config Release -j"$(nproc)" --target llama-server

curl -fL -o /opt/model.gguf "{MODEL_URL}"

cat >/etc/systemd/system/inference.service <<'UNIT'
[Unit]
Description=llama.cpp inference server
After=network-online.target

[Service]
ExecStart=/opt/llama.cpp/build/bin/llama-server \\
  --model /opt/model.gguf --host 0.0.0.0 --port {INFERENCE_PORT} --ctx-size 2048
Restart=always

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now inference
"""


class ProviderStack(cdk.Stack):
    """VPC A: inference host, internal NLB, and the endpoint service."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = ec2.Vpc(
            self,
            "ProviderVpc",
            max_azs=2,
            nat_gateways=1,  # bootstrap egress only
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        host_sg = ec2.SecurityGroup(
            self,
            "InferenceHostSg",
            vpc=vpc,
            description="Inference host: reachable only from the NLB",
            allow_all_outbound=True,
        )
        nlb_sg = ec2.SecurityGroup(
            self,
            "NlbSg",
            vpc=vpc,
            description="Internal NLB: reachable from the endpoint service only",
            allow_all_outbound=True,
        )
        nlb_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(SERVICE_PORT),
            "traffic from the VPC endpoint service on the service port",
        )
        host_sg.add_ingress_rule(
            ec2.Peer.security_group_id(nlb_sg.security_group_id),
            ec2.Port.tcp(INFERENCE_PORT),
            "inference traffic from the internal NLB",
        )

        # SSM so the host can be inspected without a bastion or a public IP.
        host_role = iam.Role(
            self,
            "InferenceHostRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")
            ],
        )

        host = ec2.Instance(
            self,
            "InferenceHost",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.C6I, ec2.InstanceSize.LARGE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=host_sg,
            role=host_role,
            user_data=ec2.UserData.custom(BOOTSTRAP),
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(30, volume_type=ec2.EbsDeviceVolumeType.GP3),
                )
            ],
        )

        nlb = elbv2.NetworkLoadBalancer(
            self,
            "InferenceNlb",
            vpc=vpc,
            internet_facing=False,  # the load-bearing line of this stack
            cross_zone_enabled=True,
            security_groups=[nlb_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )

        target_group = elbv2.NetworkTargetGroup(
            self,
            "InferenceTargetGroup",
            vpc=vpc,
            port=INFERENCE_PORT,
            protocol=elbv2.Protocol.TCP,
            health_check=elbv2.HealthCheck(
                protocol=elbv2.Protocol.TCP,
                port=str(INFERENCE_PORT),
                interval=Duration.seconds(30),
                healthy_threshold_count=2,
            ),
        )
        target_group.add_target(elbv2_targets.InstanceTarget(host, port=INFERENCE_PORT))

        nlb.add_listener(
            "InferenceListener",
            port=SERVICE_PORT,
            protocol=elbv2.Protocol.TCP,
            default_target_groups=[target_group],
        )

        # acceptance_required=False so the same account can attach without a manual
        # approval. Cross-account consumers would set allowed_principals instead.
        self.service = ec2.VpcEndpointService(
            self,
            "InferenceEndpointService",
            vpc_endpoint_service_load_balancers=[nlb],
            acceptance_required=False,
        )

        # Flow logs so the egress claim is measurable, not asserted. ONE_MINUTE
        # aggregation gives a window fine enough to separate inference from the
        # bootstrap burst; ALL traffic so the report can distinguish the private
        # subnet's internet-bound bytes from intra-VPC and NLB traffic.
        flow_log_group = logs.LogGroup(
            self,
            "ProviderFlowLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        ec2.FlowLog(
            self,
            "VpcFlowLog",
            resource_type=ec2.FlowLogResourceType.from_vpc(vpc),
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(log_group=flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.ALL,
            max_aggregation_interval=ec2.FlowLogMaxAggregationInterval.ONE_MINUTE,
            # Custom format — only the fields the egress report reads, in a fixed
            # order the parser relies on. Smaller records than the full default.
            log_format=[
                ec2.LogFormat.SRC_ADDR,
                ec2.LogFormat.DST_ADDR,
                ec2.LogFormat.BYTES,
                ec2.LogFormat.ACTION,
                ec2.LogFormat.LOG_STATUS,
                ec2.LogFormat.START_TIMESTAMP,
            ],
        )

        # The private (PRIVATE_WITH_EGRESS) subnets are where the inference host
        # lives; their CIDRs are what the egress report filters srcaddr against.
        private_cidrs = [sub.ipv4_cidr_block for sub in vpc.private_subnets]

        CfnOutput(self, "ServiceName", value=self.service.vpc_endpoint_service_name)
        CfnOutput(self, "InferenceHostPrivateDns", value=host.instance_private_dns_name)
        CfnOutput(self, "FlowLogGroup", value=flow_log_group.log_group_name)
        CfnOutput(self, "ProviderVpcCidr", value=vpc.vpc_cidr_block)
        CfnOutput(
            self,
            "InferenceSubnetCidrs",
            value=cdk.Fn.join(",", private_cidrs),
        )
