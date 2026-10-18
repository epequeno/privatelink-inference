"""Probe configuration, read from the environment.

No account ids, ARNs or endpoint ids here: those come from the stack outputs at
run time and are never committed.
"""

import os

from pydantic import BaseModel

DEFAULT_REGION = "us-east-1"


class Settings(BaseModel):
    """Values the probe needs at run time."""

    region: str = DEFAULT_REGION
    service_name: str = ""
    endpoint_dns_name: str = ""
    probe_function_name: str = ""
    flow_log_group: str = ""
    provider_vpc_cidr: str = ""
    inference_subnet_cidrs: str = ""
    # SageMaker stage — values come from the stack outputs, as above.
    sagemaker_probe_function_name: str = ""
    sagemaker_artifact_bucket: str = ""
    sagemaker_training_role_arn: str = ""
    sagemaker_model_package_group: str = ""
    sagemaker_hourly_rate: str = ""


def load() -> Settings:
    """Build settings from the process environment."""
    return Settings(
        region=os.environ.get("AWS_REGION", DEFAULT_REGION),
        service_name=os.environ.get("PRIVATELINK_SERVICE_NAME", ""),
        endpoint_dns_name=os.environ.get("ENDPOINT_DNS_NAME", ""),
        probe_function_name=os.environ.get("PROBE_FUNCTION_NAME", ""),
        flow_log_group=os.environ.get("FLOW_LOG_GROUP", ""),
        provider_vpc_cidr=os.environ.get("PROVIDER_VPC_CIDR", ""),
        inference_subnet_cidrs=os.environ.get("INFERENCE_SUBNET_CIDRS", ""),
        sagemaker_probe_function_name=os.environ.get("SAGEMAKER_PROBE_FUNCTION_NAME", ""),
        sagemaker_artifact_bucket=os.environ.get("SAGEMAKER_ARTIFACT_BUCKET", ""),
        sagemaker_training_role_arn=os.environ.get("SAGEMAKER_TRAINING_ROLE_ARN", ""),
        sagemaker_model_package_group=os.environ.get("SAGEMAKER_MODEL_PACKAGE_GROUP", ""),
        sagemaker_hourly_rate=os.environ.get("SAGEMAKER_HOURLY_RATE", ""),
    )


settings = load()
