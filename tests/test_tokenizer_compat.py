"""The tokenizer-config shim that makes the trained artifact loadable by TGI."""

import json
from pathlib import Path

from training.tokenizer_compat import normalize_tokenizer_config


def _write(tmp_path: Path, config: dict) -> Path:
    (tmp_path / "tokenizer_config.json").write_text(json.dumps(config))
    return tmp_path


def test_list_form_is_rewritten_to_a_mapping(tmp_path: Path) -> None:
    model_dir = _write(tmp_path, {"extra_special_tokens": ["<|im_start|>", "<|im_end|>"]})
    assert normalize_tokenizer_config(model_dir) is True
    config = json.loads((model_dir / "tokenizer_config.json").read_text())
    assert config["extra_special_tokens"] == {
        "<|im_start|>": "<|im_start|>",
        "<|im_end|>": "<|im_end|>",
    }


def test_mapping_is_left_alone(tmp_path: Path) -> None:
    # A tokenizer saved by transformers 4.x is already correct; rewriting it
    # would be a silent no-op at best and a corruption at worst.
    model_dir = _write(tmp_path, {"extra_special_tokens": {"a": "a"}})
    assert normalize_tokenizer_config(model_dir) is False


def test_missing_key_and_missing_file_are_not_errors(tmp_path: Path) -> None:
    assert normalize_tokenizer_config(_write(tmp_path, {"model_max_length": 10})) is False
    assert normalize_tokenizer_config(tmp_path / "absent") is False
