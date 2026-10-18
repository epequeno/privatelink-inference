"""SageMaker stack — the managed-host option, reached the same private way.

The repo's claim is that the model has no public route. That claim is extended
here to a managed AWS service: the endpoint lives in SageMaker, the consumer VPC
reaches it through the ``sagemaker.runtime`` interface endpoint (see
``stacks/consumer.py``), and the consumer VPC still has no internet route at all.

Two phases, because an endpoint cannot exist before a model does:

1. ``cdk deploy`` with no ``model_package_arn`` context creates the artifact
   bucket, the two execution roles and the Model Registry package group.
2. ``just sagemaker-train`` runs the job and registers a model package;
   ``just sagemaker-deploy`` writes that ARN into ``infra/cdk.context.json`` and
   re-deploys this stack, which adds the model, endpoint config and endpoint.

Nothing in this file reads the network or the account, so ``cdk synth`` stays
offline and account-free. The DLC account (763104351884) is AWS's public one.
"""

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sagemaker as sagemaker
from aws_cdk import aws_sns as sns
from constructs import Construct

# Qwen2.5-0.5B-Instruct — the same model the llama.cpp baseline hosts, so the
# comparison in RESULTS.md isolates the hosting path.
TRAINING_IMAGE = (
    "763104351884.dkr.ecr.us-east-1.amazonaws.com/huggingface-pytorch-training:"
    "2.9.0-transformers5.3.0-gpu-py312-cu130-ubuntu22.04"
)
TGI_IMAGE = (
    "763104351884.dkr.ecr.us-east-1.amazonaws.com/huggingface-pytorch-tgi-inference:"
    "2.7.0-tgi3.3.6-gpu-py311-cu124-ubuntu22.04-v1.2"
)
DLC_ACCOUNT = "763104351884"
INSTANCE_TYPE = "ml.g5.xlarge"  # 1x A10G 24 GB — the same GPU family the anchor run used
ENDPOINT_NAME = "qwen-legal-lora"  # fixed: the probe Lambda is configured with this name
MODEL_PACKAGE_GROUP = "qwen-legal-lora"


class SageMakerStack(cdk.Stack):
    """Artifact bucket, execution roles, Model Registry, and (phase 2) the endpoint."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        model_package_arn: str | None = None,
        alarm_email: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.endpoint_defined = model_package_arn is not None
        # Fixed in both phases: the consumer stack points its probe at this name
        # even before the endpoint exists, so the two stacks never need to be
        # deployed in one order to be coherent.
        self.endpoint_name = ENDPOINT_NAME

        # Training reads the corpus and writes the merged model; hosting reads the
        # merged model. Public access blocked, and objects expire because every
        # artifact here is reproducible by re-running the job.
        artifacts = s3.Bucket(
            self,
            "Artifacts",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
        )

        def execution_role(name: str, *, read_only: bool) -> iam.Role:
            role = iam.Role(
                self,
                name,
                assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
                description="SageMaker execution role: this stack's bucket and DLC images only",
            )
            if read_only:
                artifacts.grant_read(role)
            else:
                artifacts.grant_read_write(role)
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="PullDlcImage",
                    actions=[
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                    ],
                    resources=[f"arn:aws:ecr:{self.region}:{DLC_ACCOUNT}:repository/*"],
                )
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="EcrAuth", actions=["ecr:GetAuthorizationToken"], resources=["*"]
                )
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="JobLogs",
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    resources=[
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/sagemaker/*"
                    ],
                )
            )
            return role

        training_role = execution_role("TrainingRole", read_only=False)
        hosting_role = execution_role("HostingRole", read_only=True)

        # The registry entry is what the endpoint is deployed from, so the lineage
        # training job → model package → endpoint is a real chain, not a diagram.
        group = sagemaker.CfnModelPackageGroup(
            self,
            "ModelPackageGroup",
            model_package_group_name=MODEL_PACKAGE_GROUP,
            model_package_group_description=(
                "Qwen2.5-0.5B-Instruct LoRA SFT on legal clause reasoning — "
                "artifact of the SageMaker training job in this repo"
            ),
        )

        if model_package_arn:
            model = sagemaker.CfnModel(
                self,
                "Model",
                execution_role_arn=hosting_role.role_arn,
                containers=[
                    sagemaker.CfnModel.ContainerDefinitionProperty(
                        # Deploying from the registry entry also carries the image
                        # and artifact URL, so neither is restated here.
                        model_package_name=model_package_arn,
                    )
                ],
            )
            endpoint_config = sagemaker.CfnEndpointConfig(
                self,
                "EndpointConfig",
                production_variants=[
                    sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                        variant_name="AllTraffic",
                        model_name=model.attr_model_name,
                        initial_instance_count=1,
                        initial_variant_weight=1.0,
                        instance_type=INSTANCE_TYPE,
                        container_startup_health_check_timeout_in_seconds=900,
                        model_data_download_timeout_in_seconds=600,
                    )
                ],
            )
            sagemaker.CfnEndpoint(
                self,
                "Endpoint",
                endpoint_name=ENDPOINT_NAME,  # deterministic: the probe is configured with it
                endpoint_config_name=endpoint_config.attr_endpoint_config_name,
            )
            # Everything below is the monitoring half of MLOps: the metrics a
            # reviewer expects for a deployed endpoint, and one alarm that would
            # page if the endpoint started failing.
            alarm_topic = sns.Topic(self, "EndpointAlarmTopic")
            if alarm_email:
                sns.Subscription(
                    self,
                    "EndpointAlarmSubscription",
                    topic=alarm_topic,
                    protocol=sns.SubscriptionProtocol.EMAIL,
                    endpoint=alarm_email,
                )
            errors = cloudwatch.Metric(
                namespace="AWS/SageMaker",
                metric_name="Invocation5XXErrors",
                dimensions_map={"EndpointName": ENDPOINT_NAME, "VariantName": "AllTraffic"},
                statistic="Sum",
                period=Duration.minutes(5),
            )
            cloudwatch.Alarm(
                self,
                "Endpoint5xxAlarm",
                metric=errors,
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ).add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

            dashboard = cloudwatch.Dashboard(self, "EndpointDashboard")
            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title="Invocations",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/SageMaker",
                            metric_name="Invocations",
                            dimensions_map={"EndpointName": ENDPOINT_NAME},
                            statistic="Sum",
                            period=Duration.minutes(1),
                        )
                    ],
                ),
                cloudwatch.GraphWidget(
                    title="Model latency (µs)",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/SageMaker",
                            metric_name="ModelLatency",
                            dimensions_map={
                                "EndpointName": ENDPOINT_NAME,
                                "VariantName": "AllTraffic",
                            },
                            statistic="Average",
                            period=Duration.minutes(1),
                        )
                    ],
                ),
                cloudwatch.GraphWidget(
                    title="5XX errors",
                    left=[errors],
                ),
            )
            CfnOutput(self, "DashboardName", value=dashboard.dashboard_name)

        CfnOutput(self, "EndpointName", value=self.endpoint_name)
        CfnOutput(self, "ArtifactBucket", value=artifacts.bucket_name)
        CfnOutput(self, "TrainingRoleArn", value=training_role.role_arn)
        CfnOutput(self, "HostingRoleArn", value=hosting_role.role_arn)
        CfnOutput(self, "ModelPackageGroupName", value=group.model_package_group_name)
        CfnOutput(self, "InstanceType", value=INSTANCE_TYPE)

        self.artifacts = artifacts
        self.training_role = training_role
        self.model_package_group = group
