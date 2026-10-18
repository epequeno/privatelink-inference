"""Regenerate ``data/sft/`` from the legal-kit SFT corpus.

The SageMaker training job in this repo runs the legal-kit LoRA recipe at reduced
scale so that the job is cheap and repeatable. The source corpus is
``~/code/legal-kit/data/sft_reasoning/{train,val}.jsonl`` (3,012 / 335 rows),
which is not committed here. This script takes a deterministic, label-stratified
sample so the committed sample can be regenerated and audited.

    uv run python scripts/sample_sft.py --source ~/code/legal-kit/data/sft_reasoning

Not part of ``just verify``: it reads a path outside the repo and is a provenance
tool, not a build step.
"""

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

SEED = 42
TRAIN_ROWS = 300
VALIDATION_ROWS = 50
# A row is kept only if it fits the 1024-token training window with room to
# spare. ~4 chars/token is a crude but safe bound for English contract text.
MAX_CHARS = 3000
LABELS = ("entailed", "contradicted", "neutral")


def load_rows(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    kept = [
        {"input": r["input"], "output": r["output"], "label": r["label"]}
        for r in rows
        if len(r["input"]) + len(r["output"]) <= MAX_CHARS and r["label"] in LABELS
    ]
    if not kept:
        raise SystemExit(f"no rows of {path} fit the {MAX_CHARS}-char bound")
    return kept


def stratified(rows: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Take ``n`` rows with the label mix of the source, deterministically."""
    by_label: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_label[row["label"]].append(row)
    for bucket in by_label.values():
        rng.shuffle(bucket)

    out: list[dict] = []
    cursor = dict.fromkeys(by_label, 0)
    while len(out) < n and any(cursor[label] < len(bucket) for label, bucket in by_label.items()):
        for label in LABELS:
            bucket = by_label.get(label, [])
            if len(out) < n and cursor[label] < len(bucket):
                out.append(bucket[cursor[label]])
                cursor[label] += 1
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.home() / "code/legal-kit/data/sft_reasoning",
        help="directory holding train.jsonl and val.jsonl",
    )
    parser.add_argument("--out", type=Path, default=Path("data/sft"))
    args = parser.parse_args()

    train = stratified(load_rows(args.source / "train.jsonl"), TRAIN_ROWS, random.Random(SEED))
    # A different seed so validation is not a subset of the training sample.
    validation = stratified(
        load_rows(args.source / "val.jsonl"), VALIDATION_ROWS, random.Random(SEED + 1)
    )

    args.out.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out / "train.jsonl", train)
    write_jsonl(args.out / "validation.jsonl", validation)

    for name, rows in (("train.jsonl", train), ("validation.jsonl", validation)):
        counts = {label: sum(r["label"] == label for r in rows) for label in LABELS}
        print(f"{args.out / name}: {len(rows)} rows {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
