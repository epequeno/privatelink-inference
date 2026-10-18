"""SageMaker stage: train, register, deploy state, probe, metrics.

Everything that touches AWS is behind a subcommand, so importing this module is
free: `just verify` imports it for the pure helpers and never opens a socket.

    train              upload code + data, run a Training Job, write results/*.json
    register           create a Model Registry entry from the job's artifact
    write-context      put the model package ARN into infra/cdk.context.json so
                       `cdk deploy` adds the endpoint (the ARN holds the account
                       id, so that file is gitignored)
    spinup             endpoint creation duration, from CloudFormation events
    probe --private    N timed InvokeEndpoint calls through the interface endpoint
    probe --public     the negative test: this VPC has no internet route
    metrics            CloudWatch endpoint metrics for the last N minutes
"""

import argparse
import json
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import boto3

from infra.stacks.sagemaker import (
    ENDPOINT_NAME,
    INSTANCE_TYPE,
    MODEL_PACKAGE_GROUP,
    TGI_IMAGE,
    TRAINING_IMAGE,
)
from probe.config import settings
from probe.probe import percentile

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
DATA_DIR = ROOT / "data" / "sft"
CODE_DIR = ROOT / "training"
CONTEXT_FILE = ROOT / "infra" / "cdk.context.json"
STACK_NAME = "PrivatelinkSageMakerStack"

DEFAULT_SAMPLES = 10
TRAIN_INSTANCE = INSTANCE_TYPE
MAX_RUNTIME_S = 3600

METRIC_DEFINITIONS = [
    {"Name": "train_loss", "Regex": r"train_loss=([0-9.]+)"},
    {"Name": "eval_loss", "Regex": r"eval_loss=([0-9.]+)"},
    {"Name": "train_seconds", "Regex": r"train_seconds=([0-9.]+)"},
]


# --- pure helpers (unit tested) ---------------------------------------------


def summarize(samples: list[dict]) -> dict:
    """p50/p95/p99 latency and mean tokens/sec from raw per-call samples."""
    latencies = [float(s["elapsed_ms"]) for s in samples]
    tps = [float(s["tokens_per_sec"]) for s in samples]
    return {
        "n": len(samples),
        "p50_latency_ms": round(percentile(latencies, 50), 1),
        "p95_latency_ms": round(percentile(latencies, 95), 1),
        "p99_latency_ms": round(percentile(latencies, 99), 1),
        "mean_tokens_per_sec": round(sum(tps) / len(tps), 1) if tps else 0.0,
    }


def cost_per_1k_usd(hourly_rate: float, p50_latency_ms: float) -> float:
    """Dollars per 1,000 requests at 100% single-stream utilisation.

    A real-time endpoint bills per instance-hour whether or not it is called, so
    the honest per-request figure depends on utilisation. This one assumes a
    single stream saturating the endpoint — the best case, and the only one that
    can be derived from a latency measurement alone.
    """
    return round(hourly_rate * 1000 * (p50_latency_ms / 1000) / 3600, 4)


def training_job_request(
    *,
    job_name: str,
    role_arn: str,
    bucket: str,
    source_uri: str,
    epochs: float,
    instance_type: str = TRAIN_INSTANCE,
) -> dict:
    """The CreateTrainingJob arguments. Kept pure so the request is reviewable."""
    return {
        "TrainingJobName": job_name,
        "AlgorithmSpecification": {
            "TrainingImage": TRAINING_IMAGE,
            "TrainingInputMode": "File",
            "MetricDefinitions": METRIC_DEFINITIONS,
        },
        "RoleArn": role_arn,
        "InputDataConfig": [
            {
                "ChannelName": "train",
                "DataSource": {
                    "S3DataSource": {
                        "S3DataType": "S3Prefix",
                        "S3Uri": f"s3://{bucket}/data/train",
                        "S3DataDistributionType": "FullyReplicated",
                    }
                },
                "ContentType": "application/jsonlines",
            },
            {
                "ChannelName": "validation",
                "DataSource": {
                    "S3DataSource": {
                        "S3DataType": "S3Prefix",
                        "S3Uri": f"s3://{bucket}/data/validation",
                        "S3DataDistributionType": "FullyReplicated",
                    }
                },
                "ContentType": "application/jsonlines",
            },
        ],
        "OutputDataConfig": {"S3OutputPath": f"s3://{bucket}/output"},
        "ResourceConfig": {
            "InstanceType": instance_type,
            "InstanceCount": 1,
            "VolumeSizeInGB": 50,
        },
        "StoppingCondition": {"MaxRuntimeInSeconds": MAX_RUNTIME_S},
        "HyperParameters": {
            "sagemaker_program": "train.py",
            "sagemaker_submit_directory": source_uri,
            "epochs": str(epochs),
        },
        "EnableNetworkIsolation": False,
        "Tags": [{"Key": "project", "Value": "privatelink-inference"}],
    }


def model_package_request(*, group: str, job_name: str, model_data_url: str) -> dict:
    """Register the job's merged model as a deployable inference package."""
    return {
        "ModelPackageGroupName": group,
        "ModelPackageDescription": (
            f"Qwen2.5-0.5B-Instruct LoRA SFT (1 epoch, 300 samples); training job {job_name}"
        ),
        "ModelApprovalStatus": "Approved",
        "InferenceSpecification": {
            "Containers": [
                {
                    "Image": TGI_IMAGE,
                    "ModelDataUrl": model_data_url,
                    # The framework field is an enum; TGI is not one of its values.
                    "Framework": "PYTORCH",
                    "FrameworkVersion": "3.3.6",
                    "NearestModelName": "Qwen2.5-0.5B-Instruct",
                    # The TGI DLC refuses to start without this ("HF_MODEL_ID must be
                    # set"), even though SageMaker has already unpacked the artifact
                    # here. Pointing it at the unpacked model keeps the container
                    # offline instead of fetching weights from the Hub.
                    "Environment": {"HF_MODEL_ID": "/opt/ml/model"},
                }
            ],
            "SupportedContentTypes": ["application/json"],
            "SupportedResponseMIMETypes": ["application/json"],
            "SupportedRealtimeInferenceInstanceTypes": [INSTANCE_TYPE],
            "SupportedTransformInstanceTypes": [INSTANCE_TYPE],
        },
        # Tags are a property of the package *group* in CDK; CreateModelPackage
        # rejects them on a version ("Tags are not supported in Model Package
        # versions"), so none are passed here.
    }


def stack_event_duration(events: list[dict], logical_id: str, prefix: str = "CREATE") -> float:
    """Seconds between a resource's <prefix>_IN_PROGRESS and <prefix>_COMPLETE.

    CloudFormation stack events are the only place the endpoint's own creation
    duration is recorded without polling.
    """
    started = completed = None
    for event in events:
        if event.get("LogicalResourceId") != logical_id:
            continue
        status = event.get("ResourceStatus", "")
        if status == f"{prefix}_IN_PROGRESS" and started is None:
            started = event["Timestamp"]
        elif status == f"{prefix}_COMPLETE":
            completed = event["Timestamp"]
    if started is None or completed is None:
        raise SystemExit(f"no {prefix} events for {logical_id} in the stack")
    return round((completed - started).total_seconds(), 1)


def s3_model_data_url(output_path: str, job_name: str) -> str:
    return f"{output_path.rstrip('/')}/{job_name}/output/model.tar.gz"


def output_prefix(output_path: str) -> str:
    """Key prefix inside the bucket. ``OutputDataConfig`` holds bucket + prefix."""
    _, _, prefix = output_path.removeprefix("s3://").partition("/")
    return prefix.rstrip("/")


# --- AWS plumbing -----------------------------------------------------------


def sagemaker_client():
    return boto3.client("sagemaker", region_name=settings.region)


def s3_client():
    return boto3.client("s3", region_name=settings.region)


def require(name: str, value: str) -> str:
    if not value:
        raise SystemExit(f"{name} is not set — run `just deploy` and fill in .env")
    return value


def upload_jsonl(local: Path, bucket: str, key: str) -> None:
    s3_client().upload_file(str(local), bucket, key)
    print(f"  s3://{bucket}/{key}")


def upload_source(bucket: str) -> str:
    """Tar `training/` to a temp file and upload it; script mode unpacks it."""
    key = "code/sourcedir.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "sourcedir.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path in sorted(CODE_DIR.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    tar.add(path, arcname=str(path.relative_to(CODE_DIR)))
        s3_client().upload_file(str(archive), bucket, key)
    uri = f"s3://{bucket}/{key}"
    print(f"  {uri}")
    return uri


def write_results(name: str, payload: dict) -> Path:
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / name
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"  wrote {path.relative_to(ROOT)}")
    return path


def train(epochs: float, wait: bool) -> int:
    bucket = require("SAGEMAKER_ARTIFACT_BUCKET", settings.sagemaker_artifact_bucket)
    role_arn = require("SAGEMAKER_TRAINING_ROLE_ARN", settings.sagemaker_training_role_arn)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    job_name = f"qwen-legal-lora-{stamp}"

    print("uploading code and data")
    source_uri = upload_source(bucket)
    upload_jsonl(DATA_DIR / "train.jsonl", bucket, "data/train/train.jsonl")
    upload_jsonl(DATA_DIR / "validation.jsonl", bucket, "data/validation/validation.jsonl")

    request = training_job_request(
        job_name=job_name,
        role_arn=role_arn,
        bucket=bucket,
        source_uri=source_uri,
        epochs=epochs,
    )
    client = sagemaker_client()
    response = client.create_training_job(**request)
    print(f"\njob {job_name}\n  {response['TrainingJobArn']}")
    print(f"  {TRAIN_INSTANCE} — roughly $1.41/h on demand, teardown is part of done")

    if not wait:
        print("\nnot waiting; `just sagemaker-train --wait` or re-run with the ARN to poll")
        return 0

    seen = ""
    started = time.perf_counter()
    while True:
        job = client.describe_training_job(TrainingJobName=job_name)
        status = job["TrainingJobStatus"]
        if status != seen:
            elapsed = time.perf_counter() - started
            print(f"  [{elapsed:6.0f}s] {status}", flush=True)
            seen = status
        if status in ("Completed", "Failed", "Stopped"):
            break
        time.sleep(20)

    if status != "Completed":
        print(f"\njob {status}: {job.get('FailureReason', 'no reason given')}")
        return 1

    # /opt/ml/output/data is tarred into <job>/output/output.tar.gz, not copied as
    # loose files: the metric definitions live in results.json inside that archive.
    output_path = job["OutputDataConfig"]["S3OutputPath"]
    archive_key = f"{output_prefix(output_path)}/{job_name}/output/output.tar.gz"
    raw = s3_client().get_object(Bucket=bucket, Key=archive_key)["Body"].read()
    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as tar:
        results = json.load(tar.extractfile("results.json"))
    run = {
        "job_name": job_name,
        "job_arn": job["TrainingJobArn"],
        "billable_seconds": round(job["BillableTimeInSeconds"], 1),
        "total_seconds": round(time.perf_counter() - started, 1),
        "instance_type": job["ResourceConfig"]["InstanceType"],
        "model_data_url": s3_model_data_url(output_path, job_name),
        "results": results,
    }
    write_results(f"sagemaker_train_{stamp}.json", run)
    print(
        f"\nmodel development: {run['billable_seconds']} billable seconds on "
        f"{run['instance_type']}, eval_loss {run['results']['eval_loss']:.4f}"
    )
    return 0


def register() -> int:
    runs = sorted(RESULTS.glob("sagemaker_train_*.json"))
    if not runs:
        print("no results/sagemaker_train_*.json — run `just sagemaker-train` first")
        return 2
    run = json.loads(runs[-1].read_text())

    client = sagemaker_client()
    job = client.describe_training_job(TrainingJobName=run["job_name"])
    model_data_url = s3_model_data_url(job["OutputDataConfig"]["S3OutputPath"], run["job_name"])
    response = client.create_model_package(
        **model_package_request(
            group=settings.sagemaker_model_package_group or MODEL_PACKAGE_GROUP,
            job_name=run["job_name"],
            model_data_url=model_data_url,
        )
    )
    arn = response["ModelPackageArn"]
    write_results(
        "model_package.json",
        {"model_package_arn": arn, "job_name": run["job_name"], "model_data_url": model_data_url},
    )
    print(f"\nregistered: {arn}")
    print("next: `just sagemaker-deploy`")
    return 0


def write_context(arn: str | None = None) -> int:
    state = RESULTS / "model_package.json"
    if arn is None:
        if not state.exists():
            print("no results/model_package.json — run `just sagemaker-register` first")
            return 2
        arn = json.loads(state.read_text())["model_package_arn"]
    context = json.loads(CONTEXT_FILE.read_text()) if CONTEXT_FILE.exists() else {}
    context["model_package_arn"] = arn
    CONTEXT_FILE.write_text(json.dumps(context, indent=2) + "\n")
    print(f"  {CONTEXT_FILE.relative_to(ROOT)}: model_package_arn set")
    return 0


def delete_packages() -> int:
    """Empty the Model Registry group so CloudFormation can delete it.

    A package group holding versions cannot be deleted, so `just down` would stop
    on the SageMaker stack with DELETE_FAILED and leave the endpoint behind.
    """
    client = sagemaker_client()
    group = settings.sagemaker_model_package_group or MODEL_PACKAGE_GROUP
    try:
        packages = client.list_model_packages(ModelPackageGroupName=group)[
            "ModelPackageSummaryList"
        ]
    except client.exceptions.ClientError as exc:
        print(f"  no package group to empty ({exc.response['Error']['Code']})")
        return 0
    for package in packages:
        arn = package["ModelPackageArn"]
        client.delete_model_package(ModelPackageName=arn)
        print(f"  deleted {arn}")
    return 0


def clear_context() -> int:
    """Drop the package ARN so a later deploy cannot recreate a destroyed endpoint."""
    if not CONTEXT_FILE.exists():
        return 0
    context = json.loads(CONTEXT_FILE.read_text())
    if context.pop("model_package_arn", None) is None:
        return 0
    CONTEXT_FILE.write_text(json.dumps(context, indent=2) + "\n")
    print("  cleared model_package_arn from infra/cdk.context.json")
    return 0


def spinup() -> int:
    cloudformation = boto3.client("cloudformation", region_name=settings.region)
    events = cloudformation.describe_stack_events(StackName=STACK_NAME)["StackEvents"]
    endpoint_seconds = stack_event_duration(events, "Endpoint")
    dashboard = [e for e in events if e["LogicalResourceId"] == "Endpoint"]
    payload = {
        "endpoint_spinup_seconds": endpoint_seconds,
        "endpoint_created": dashboard[-1]["Timestamp"].isoformat() if dashboard else None,
    }
    write_results("sagemaker_spinup.json", payload)
    print(f"endpoint spin-up (CFN CREATE_IN_PROGRESS → CREATE_COMPLETE): {endpoint_seconds} s")
    return 0


def invoke_probe(samples: int) -> dict:
    function_name = require("SAGEMAKER_PROBE_FUNCTION_NAME", settings.sagemaker_probe_function_name)
    client = boto3.client("lambda", region_name=settings.region)
    response = client.invoke(
        FunctionName=function_name,
        Payload=json.dumps({"samples": samples}).encode(),
    )
    return json.loads(response["Payload"].read() or b"{}")


def probe(private: bool, samples: int, rate: float) -> int:
    payload = invoke_probe(samples)
    dns = payload.get("dns", {})
    internet = payload.get("internet", {})

    print(f"endpoint: {payload.get('endpoint')}")
    print(f"dns      : {dns.get('host')} → {dns.get('addresses') or dns.get('error')}")
    if internet.get("reachable"):
        print(f"internet : REACHABLE in {internet.get('elapsed_ms')} ms — claim is false")
    else:
        print(f"internet : unreachable — {internet.get('error')}")
        print(f"           ({internet.get('elapsed_ms')} ms)")

    if not private:
        print()
        if internet.get("reachable"):
            print("FAIL — the consumer VPC can reach the internet. Do not claim privacy.")
            return 1
        print("PASS — the same VPC that invokes the endpoint has no internet route.")
        return 0

    if not payload.get("ok"):
        print(json.dumps(payload, indent=2))
        print("\nPRIVATE PATH FAILED — that is the finding, not a missing number")
        return 1

    summary = summarize(payload["samples"])
    line = (
        f"\np50 {summary['p50_latency_ms']} ms · p95 {summary['p95_latency_ms']} ms · "
        f"p99 {summary['p99_latency_ms']} ms · {summary['mean_tokens_per_sec']} tok/s "
        f"(n={summary['n']})"
    )
    print(line)
    if rate > 0:
        per_1k = cost_per_1k_usd(rate, summary["p50_latency_ms"])
        print(f"${per_1k}/1k requests at ${rate}/h and 100% single-stream utilisation")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    write_results(
        f"sagemaker_probe_{stamp}.json",
        {
            "summary": summary,
            "hourly_rate_usd": rate,
            "cost_per_1k_usd": cost_per_1k_usd(rate, summary["p50_latency_ms"]) if rate else None,
            "dns": dns,
            "internet": internet,
            "warmup": payload.get("warmup"),
            "samples": payload["samples"],
        },
    )
    return 0


def metrics(minutes: int) -> int:
    cloudwatch = boto3.client("cloudwatch", region_name=settings.region)
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)
    queries = []
    for name, statistic in (
        ("Invocations", "Sum"),
        ("ModelLatency", "Average"),
        ("OverheadLatency", "Average"),
        ("Invocation5XXErrors", "Sum"),
        ("Invocation4XXErrors", "Sum"),
    ):
        queries.append(
            {
                "Id": name.lower(),
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/SageMaker",
                        "MetricName": name,
                        "Dimensions": [
                            {"Name": "EndpointName", "Value": ENDPOINT_NAME},
                            {"Name": "VariantName", "Value": "AllTraffic"},
                        ],
                    },
                    "Period": 60,
                    "Stat": statistic,
                },
                "Label": f"{name} ({statistic})",
            }
        )
    response = cloudwatch.get_metric_data(
        MetricDataQueries=queries, StartTime=start, EndTime=end, ScanBy="TimestampAscending"
    )
    print(f"last {minutes} minutes, endpoint {ENDPOINT_NAME}")
    for result in response["MetricDataResults"]:
        values = result["Values"]
        if not values:
            print(f"  {result['Label']:28} no datapoints")
            continue
        print(
            f"  {result['Label']:28} datapoints={len(values):3} "
            f"sum={sum(values):.1f} last={values[-1]:.1f}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SageMaker stage for privatelink-inference")
    sub = parser.add_subparsers(dest="command", required=True)

    p_train = sub.add_parser("train", help="run a SageMaker Training Job")
    p_train.add_argument("--epochs", type=float, default=1.0)
    p_train.add_argument("--wait", action="store_true")

    sub.add_parser("register", help="create a Model Registry entry from the last job")

    p_context = sub.add_parser("write-context", help="record the package ARN for cdk")
    p_context.add_argument("--arn")
    sub.add_parser("clear-context", help="drop the package ARN after teardown")
    sub.add_parser("delete-packages", help="empty the registry group before destroy")

    sub.add_parser("spinup", help="measure endpoint creation duration")

    p_probe = sub.add_parser("probe", help="probe the endpoint from inside the VPC")
    group = p_probe.add_mutually_exclusive_group(required=True)
    group.add_argument("--private", action="store_true", help="timed InvokeEndpoint calls")
    group.add_argument("--public", action="store_true", help="prove this VPC has no internet")
    p_probe.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    p_probe.add_argument(
        "--rate",
        type=float,
        default=float(settings.sagemaker_hourly_rate or 0),
        help="endpoint list price in USD/hour, for the $/1k figure",
    )

    p_metrics = sub.add_parser("metrics", help="CloudWatch endpoint metrics")
    p_metrics.add_argument("--minutes", type=int, default=30)

    args = parser.parse_args(argv)
    if args.command == "train":
        return train(args.epochs, args.wait)
    if args.command == "register":
        return register()
    if args.command == "write-context":
        return write_context(args.arn)
    if args.command == "clear-context":
        return clear_context()
    if args.command == "delete-packages":
        return delete_packages()
    if args.command == "spinup":
        return spinup()
    if args.command == "probe":
        return probe(args.private, args.samples, args.rate)
    return metrics(args.minutes)


if __name__ == "__main__":
    sys.exit(main())
