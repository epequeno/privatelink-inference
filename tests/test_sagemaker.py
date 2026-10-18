"""SageMaker stage: request shape, statistics and cost arithmetic. No AWS calls."""

from datetime import UTC, datetime, timedelta

import pytest

from infra.stacks.sagemaker import ENDPOINT_NAME, INSTANCE_TYPE, TGI_IMAGE, TRAINING_IMAGE
from probe.sagemaker import (
    cost_per_1k_usd,
    model_package_request,
    output_prefix,
    s3_model_data_url,
    stack_event_duration,
    summarize,
    training_job_request,
)


def _samples(*elapsed):
    return [
        {"elapsed_ms": ms, "completion_tokens": 10, "tokens_per_sec": round(10 / (ms / 1000), 1)}
        for ms in elapsed
    ]


def test_summarize_percentiles_and_throughput() -> None:
    summary = summarize(_samples(100, 200, 300, 400, 500))
    assert summary["n"] == 5
    assert summary["p50_latency_ms"] == 300.0
    assert summary["p95_latency_ms"] == 480.0
    assert summary["p99_latency_ms"] == 496.0
    # Mean of per-call rates, not tokens over mean latency: (100+50+33+25+20)/5.
    assert summary["mean_tokens_per_sec"] == pytest.approx(45.7, abs=0.2)


def test_summarize_empty_is_zero_not_a_crash() -> None:
    # A failed Lambda returns no samples; the caller reports the error separately.
    assert summarize([])["n"] == 0
    assert summarize([])["p50_latency_ms"] == 0.0


def test_cost_per_1k_assumes_full_single_stream_utilisation() -> None:
    # 1 h at $1.41, 250 ms per request → 14,400 requests/h → $0.0979 per 1k.
    assert cost_per_1k_usd(1.41, 250) == pytest.approx(0.0979, abs=1e-4)
    assert cost_per_1k_usd(0, 250) == 0


def test_training_request_wires_both_channels_and_the_dlc_image() -> None:
    request = training_job_request(
        job_name="job-1",
        role_arn="arn:aws:iam::111122223333:role/training",
        bucket="artifacts",
        source_uri="s3://artifacts/code/sourcedir.tar.gz",
        epochs=1,
    )
    assert request["AlgorithmSpecification"]["TrainingImage"] == TRAINING_IMAGE
    channels = {c["ChannelName"]: c for c in request["InputDataConfig"]}
    assert set(channels) == {"train", "validation"}
    assert channels["train"]["DataSource"]["S3DataSource"]["S3Uri"] == "s3://artifacts/data/train"
    assert request["ResourceConfig"]["InstanceType"] == INSTANCE_TYPE
    assert request["HyperParameters"]["sagemaker_program"] == "train.py"
    assert request["HyperParameters"]["epochs"] == "1"
    # The job must finish or stop on its own; a hung job bills until it is killed.
    assert 0 < request["StoppingCondition"]["MaxRuntimeInSeconds"] <= 3600


def test_model_package_deploys_the_registry_entry_not_the_training_image() -> None:
    request = model_package_request(
        group="qwen-legal-lora",
        job_name="job-1",
        model_data_url="s3://artifacts/output/job-1/output/model.tar.gz",
    )
    container = request["InferenceSpecification"]["Containers"][0]
    assert container["Image"] == TGI_IMAGE
    assert container["Image"] != TRAINING_IMAGE
    assert container["ModelDataUrl"].endswith("model.tar.gz")
    assert request["InferenceSpecification"]["SupportedRealtimeInferenceInstanceTypes"] == [
        INSTANCE_TYPE
    ]
    assert request["ModelApprovalStatus"] == "Approved"


def test_model_data_url_joins_once() -> None:
    assert s3_model_data_url("s3://b/output/", "job") == "s3://b/output/job/output/model.tar.gz"
    assert s3_model_data_url("s3://b/output", "job") == "s3://b/output/job/output/model.tar.gz"


def test_output_prefix_drops_bucket_keeps_prefix() -> None:
    # OutputDataConfig holds bucket *and* prefix; the key inside the bucket does not.
    assert output_prefix("s3://bucket/output") == "output"
    assert output_prefix("s3://bucket/output/") == "output"
    assert output_prefix("s3://bucket") == ""


def test_endpoint_name_is_stable_and_legal() -> None:
    # The probe Lambda is configured with this name, so it cannot be CDK-generated.
    assert ENDPOINT_NAME == "qwen-legal-lora"
    assert len(ENDPOINT_NAME) <= 63


def _event(logical_id, status, seconds):
    base = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    return {
        "LogicalResourceId": logical_id,
        "ResourceStatus": status,
        "Timestamp": base + timedelta(seconds=seconds),
    }


def test_stack_event_duration_measures_the_endpoint_only() -> None:
    events = [
        _event("Endpoint", "CREATE_IN_PROGRESS", 0),
        _event("Model", "CREATE_COMPLETE", 30),
        _event("Endpoint", "CREATE_COMPLETE", 245),
    ]
    assert stack_event_duration(events, "Endpoint") == 245.0


def test_stack_event_duration_without_events_is_an_error() -> None:
    # Better to fail than to report a fabricated spin-up number.
    with pytest.raises(SystemExit):
        stack_event_duration([], "Endpoint")
