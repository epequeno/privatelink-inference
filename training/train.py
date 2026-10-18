"""SageMaker Training Job entry point (script mode).

Runs the legal-kit LoRA SFT recipe at reduced scale: Qwen2.5-0.5B-Instruct, LoRA
r=16, one epoch, a few hundred samples. The point is a real training job ARN, a
real billable duration and a real registered artifact — not a good model.

The job writes:
  /opt/ml/model/           merged fp16 weights + tokenizer (SageMaker tars this to S3)
  /opt/ml/output/data/     results.json (SageMaker copies this to the same prefix)

Hyperparameters arrive as ``SM_HP_<NAME>`` environment variables, which is what
SageMaker does for script mode.
"""

import json
import os
import time
from pathlib import Path

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from tokenizer_compat import normalize_tokenizer_config
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import SFTConfig, SFTTrainer

BASE_MODEL = os.environ.get("SM_HP_BASE_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
EPOCHS = float(os.environ.get("SM_HP_EPOCHS", "1"))
LR = float(os.environ.get("SM_HP_LEARNING_RATE", "2e-5"))
MAX_LENGTH = int(os.environ.get("SM_HP_MAX_LENGTH", "1024"))
BATCH = int(os.environ.get("SM_HP_BATCH_SIZE", "4"))
GRAD_ACCUM = int(os.environ.get("SM_HP_GRAD_ACCUM", "4"))
LORA_R = int(os.environ.get("SM_HP_LORA_R", "16"))
LORA_ALPHA = int(os.environ.get("SM_HP_LORA_ALPHA", "32"))
SEED = int(os.environ.get("SM_HP_SEED", "42"))

SYSTEM_MSG = (
    "You are a legal reasoning expert. Given a passage from a legal contract "
    "and a claim about it, reason step-by-step about the relationship, then classify it. "
    "Output your reasoning in  thinking tags and your answer in <answer> tags."
)


def load_jsonl(directory: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(Path(directory).glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    if not rows:
        raise SystemExit(f"no .jsonl rows under {directory}")
    return rows


def to_messages(row: dict) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": row["input"]},
            {"role": "assistant", "content": row["output"]},
        ]
    }


def main() -> int:
    model_dir = Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
    output_dir = Path(os.environ.get("SM_OUTPUT_DATA_DIR", "/opt/ml/output/data"))
    work_dir = Path(os.environ.get("SM_CHANNEL_WORK", output_dir / "checkpoints"))
    model_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    train_rows = load_jsonl(os.environ["SM_CHANNEL_TRAIN"])
    val_rows = load_jsonl(os.environ["SM_CHANNEL_VALIDATION"])
    print(f"train rows: {len(train_rows)}  validation rows: {len(val_rows)}", flush=True)

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL, dtype=torch.bfloat16)
    model = get_peft_model(
        model,
        LoraConfig(
            r=LORA_R,
            lora_alpha=LORA_ALPHA,
            target_modules=[
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
            ],
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
        ),
    )
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"trainable params: {trainable:,} / {total:,} ({trainable / total:.2%})", flush=True)

    args = SFTConfig(
        output_dir=str(work_dir),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH,
        per_device_eval_batch_size=BATCH,
        gradient_accumulation_steps=GRAD_ACCUM,
        learning_rate=LR,
        weight_decay=0.01,
        warmup_ratio=0.1,
        lr_scheduler_type="cosine",
        logging_steps=5,
        eval_strategy="no",
        save_strategy="no",  # only the merged model is written; checkpoints cost S3 and time
        bf16=True,
        max_length=MAX_LENGTH,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to="none",
        seed=SEED,
    )

    trainer = SFTTrainer(
        model=model,
        args=args,
        train_dataset=Dataset.from_list([to_messages(r) for r in train_rows]),
        eval_dataset=Dataset.from_list([to_messages(r) for r in val_rows]),
        processing_class=tokenizer,
    )

    started = time.perf_counter()
    result = trainer.train()
    train_seconds = time.perf_counter() - started

    eval_result = trainer.evaluate()
    eval_seconds = time.perf_counter() - started - train_seconds

    # Merge the adapter into the base weights: the serving container then loads a
    # plain HF checkpoint and needs no PEFT machinery at inference time.
    merged = trainer.model.merge_and_unload()
    merged.save_pretrained(model_dir, safe_serialization=True)
    tokenizer.save_pretrained(model_dir)
    if normalize_tokenizer_config(model_dir):
        print("rewrote extra_special_tokens for the TGI serving image", flush=True)

    results = {
        "base_model": BASE_MODEL,
        "train_samples": len(train_rows),
        "validation_samples": len(val_rows),
        "train_loss": result.training_loss,
        "eval_loss": eval_result["eval_loss"],
        "epochs": EPOCHS,
        "lora_r": LORA_R,
        "lora_alpha": LORA_ALPHA,
        "learning_rate": LR,
        "effective_batch_size": BATCH * GRAD_ACCUM,
        "max_length": MAX_LENGTH,
        "trainable_params": trainable,
        "total_params": total,
        "train_seconds": round(train_seconds, 1),
        "eval_seconds": round(eval_seconds, 1),
        "trainer_metrics": {k: v for k, v in result.metrics.items() if isinstance(v, (int, float))},
    }
    (output_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")

    # Metric definitions on the job parse these lines into CloudWatch metrics.
    print(f"train_loss={results['train_loss']:.6f}", flush=True)
    print(f"eval_loss={results['eval_loss']:.6f}", flush=True)
    print(f"train_seconds={train_seconds:.1f}", flush=True)
    print(json.dumps(results, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
