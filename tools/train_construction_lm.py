"""Fine-tune a small LM on driven construction sequences. PROTOTYPE.

Trains on the reversible text form of `driven_block_visits` sequences:
tracks whose block order was WATCHED, not inferred. The question this
prototype answers is narrow and worth stating plainly: does 1,231
sequences of this shape teach a small model to emit syntactically valid,
non-degenerate construction streams? Map quality is a later question and
is NOT claimed here.

Split is BY MAP, never by sequence. Up to six leaderboard lines share a
map, and the same map's blocks in train and eval would leak the answer:
eval loss would measure memorisation of a layout the model already saw.

Defaults target 16 GB VRAM: a ~0.5B pretrained model, LoRA adapters,
bf16, gradient checkpointing. Pretrained rather than from scratch
because 91k tokens is far too little to learn a language from zero;
what transfers is the sequence-modelling machinery, not the game.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Import the leaf module directly: src.learning.__init__ pulls in
# DB-backed siblings (pymysql), which the training venv has no
# reason to carry.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "construction_text",
    Path(__file__).resolve().parents[1] / "src" / "learning"
    / "construction_text.py")
_ct = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_ct)
serialize = _ct.serialize

_LOG = logging.getLogger("train-lm")


def build_split(corpus: Path, eval_frac: float, seed: int):
    """Group sequences by map, then split the MAPS."""
    by_map: dict[str, list[str]] = {}
    for line in corpus.open(encoding="utf-8"):
        doc = json.loads(line)
        uid = (doc.get("map_uid") or "?").split("#")[0]
        text = serialize(doc["tokens"])
        by_map.setdefault(uid, []).append(text)

    maps = sorted(by_map)
    random.Random(seed).shuffle(maps)
    n_eval = max(1, int(len(maps) * eval_frac))
    eval_maps = set(maps[:n_eval])
    train, ev = [], []
    for uid, texts in by_map.items():
        (ev if uid in eval_maps else train).extend(texts)
    _LOG.info(
        "split by map: %d train seqs / %d eval seqs (%d / %d maps)",
        len(train), len(ev), len(maps) - len(eval_maps), len(eval_maps),
    )
    return train, ev


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path,
                    default=Path("data/artifacts/telemetry/construction_sequences_v0.2.jsonl"))
    ap.add_argument("--model", type=str, default="Qwen/Qwen2.5-0.5B")
    ap.add_argument("--out", type=Path, default=Path("data/artifacts/models/construction-lm-v0"))
    ap.add_argument("--epochs", type=float, default=6.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--eval-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--dump-split", type=Path, default=None,
                    help="write the split as JSONL and exit (no training)")
    args = ap.parse_args()

    train_texts, eval_texts = build_split(args.corpus, args.eval_frac, args.seed)

    if args.dump_split:
        args.dump_split.parent.mkdir(parents=True, exist_ok=True)
        with args.dump_split.open("w", encoding="utf-8") as fh:
            for t in train_texts:
                fh.write(json.dumps({"split": "train", "text": t}) + "\n")
            for t in eval_texts:
                fh.write(json.dumps({"split": "eval", "text": t}) + "\n")
        _LOG.info("wrote split to %s", args.dump_split)
        return 0

    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM, AutoTokenizer, DataCollatorForLanguageModeling,
        Trainer, TrainingArguments,
    )

    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def encode(batch):
        out = tok(
            [t + tok.eos_token for t in batch["text"]],
            truncation=True, max_length=args.max_len,
        )
        return out

    ds_train = Dataset.from_dict({"text": train_texts}).map(
        encode, batched=True, remove_columns=["text"])
    ds_eval = Dataset.from_dict({"text": eval_texts}).map(
        encode, batched=True, remove_columns=["text"])

    model = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype=torch.bfloat16, device_map="cuda:0")
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(
        r=32, lora_alpha=64, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    ))
    model.print_trainable_parameters()

    args.out.mkdir(parents=True, exist_ok=True)
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(args.out),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch,
            gradient_accumulation_steps=args.accum,
            learning_rate=args.lr,
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            logging_steps=10,
            eval_strategy="epoch",
            save_strategy="epoch",
            save_total_limit=2,
            bf16=True,
            report_to=[],
            seed=args.seed,
        ),
        train_dataset=ds_train,
        eval_dataset=ds_eval,
        data_collator=DataCollatorForLanguageModeling(tok, mlm=False),
    )
    trainer.train()
    trainer.save_model(str(args.out / "final"))
    tok.save_pretrained(str(args.out / "final"))
    metrics = trainer.evaluate()
    _LOG.info("final eval: %s", metrics)
    (args.out / "final" / "eval.json").write_text(
        json.dumps(metrics, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
