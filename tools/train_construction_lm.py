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
    ap.add_argument("--drop-overlong", action="store_true",
                    help="drop sequences longer than max-len instead of "
                         "truncating them (truncation clips the Finish "
                         "and teaches 'end anywhere')")
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
    # Do NOT set pad_token = eos_token here.
    #
    # MEASURED: with pad == eos, DataCollatorForLanguageModeling masks
    # every pad position out of the labels, which masks the GENUINE
    # trailing EOS as well. Verified directly: zero eos ids survive in
    # the label tensor, so the model is never taught to stop. That is
    # why v0.1 and v0.3 both rambled to the token cap on 20+ of 24
    # samples while 83% of the corpus is short sequences, and why 5x
    # the data changed the length not at all.
    #
    # A distinct pad token keeps the two roles separate: padding is
    # ignored, the real end-of-sequence is learned.
    if tok.pad_token is None or tok.pad_token_id == tok.eos_token_id:
        tok.add_special_tokens({"pad_token": "<|pad|>"})

    if args.drop_overlong:
        # Drop whole sequences that exceed max_len instead of letting
        # the encoder truncate them. Truncation is the one thing the
        # raw-map corpus must never meet: a map cut mid-sequence loses
        # its Finish and teaches "end anywhere" — the exact defect the
        # 2026-07-29 in-game run traced to the telemetry corpus (69% of
        # NotValidable verdicts were missing-finish). Losing the 0.1%
        # longest maps entirely is the cheaper bias.
        def fits(t):
            return len(tok(t + tok.eos_token)["input_ids"]) <= args.max_len

        n_tr, n_ev = len(train_texts), len(eval_texts)
        train_texts = [t for t in train_texts if fits(t)]
        eval_texts = [t for t in eval_texts if fits(t)]
        _LOG.info("drop-overlong: removed %d train / %d eval sequences",
                  n_tr - len(train_texts), n_ev - len(eval_texts))

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
    # Only ever GROW. Qwen ships an embedding matrix padded well past
    # the tokenizer length (151,936 rows vs 151,665 tokens), so resizing
    # to len(tok) SHRINKS it and the checkpoint then refuses to load
    # against a stock base model. The new pad id fits in the existing
    # rows, so usually nothing needs to happen at all.
    if len(tok) > model.get_input_embeddings().weight.shape[0]:
        model.resize_token_embeddings(len(tok))
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(
        r=32, lora_alpha=64, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    ))
    model.print_trainable_parameters()

    class ChunkedLossTrainer(Trainer):
        """Cross-entropy in checkpointed chunks along the sequence.

        MEASURED (v0.5, 2026-07-29): HF's stock causal-LM loss
        materialises the full fp32 logits for the whole sequence —
        seq_len x 151,936 vocab — which spiked 16.05 GiB at 4,096
        tokens and 8.97 GiB at 2,048, OOMing a 16 GB card twice (and
        `expandable_segments` is not honoured on Windows, so 6.6 GiB
        of fragmentation stacked on top). The model forward itself is
        fine; only the loss upcast explodes.

        Chunking alone would NOT fix it: every chunk's .float() copy
        stays alive in the autograd graph, same peak. Wrapping each
        chunk in torch.utils.checkpoint frees the fp32 copy after its
        partial sum and recomputes it during backward, so the peak is
        one 256-token chunk (~0.16 GiB) instead of the whole sequence.
        Token-sum / valid-count normalisation, same semantics as the
        stock loss for batch size 1.
        """

        def compute_loss(self, model, inputs, return_outputs=False,
                         **kwargs):
            import torch.utils.checkpoint as _ckpt
            labels = inputs.pop("labels")
            outputs = model(**inputs)
            shift_logits = outputs.logits[:, :-1, :]
            shift_labels = labels[:, 1:]
            valid = (shift_labels != -100).sum().clamp(min=1)

            def chunk_ce(lg, lb):
                return torch.nn.functional.cross_entropy(
                    lg.float().reshape(-1, lg.shape[-1]), lb.reshape(-1),
                    ignore_index=-100, reduction="sum")

            chunk = 256
            loss = shift_logits.new_zeros((), dtype=torch.float32)
            for i in range(0, shift_logits.shape[1], chunk):
                lb = shift_labels[:, i:i + chunk]
                if (lb != -100).any():
                    loss = loss + _ckpt.checkpoint(
                        chunk_ce, shift_logits[:, i:i + chunk], lb,
                        use_reentrant=False)
            loss = loss / valid
            return (loss, outputs) if return_outputs else loss

    from transformers import TrainerCallback

    class EmptyCacheCallback(TrainerCallback):
        """Release cached allocator blocks every N steps.

        MEASURED (v0.5 third launch): with wildly varying sequence
        lengths (p50 947 / max 4571 tokens) the caching allocator
        fragments until a long sequence no longer fits contiguously,
        Windows WDDM then silently spills to SHARED memory, and step
        time goes 10 s -> 436 s without any error. Flushing the cache
        periodically costs milliseconds and keeps allocations packed.
        """

        def on_step_end(self, targs, state, control, **kwargs):
            # With pad_to_multiple_of quantising shapes, flushes are
            # insurance, not the fix — rare on purpose, since each one
            # forces slow re-warming cudaMallocs on the next step.
            if state.global_step % 100 == 0:
                torch.cuda.empty_cache()
            if state.global_step % 50 == 0:
                # Telemetry for the spill failure mode: if reserved
                # runs far ahead of allocated, fragmentation is
                # rebuilding and a silent shared-memory crawl is next.
                _LOG.info(
                    "vram step %d: allocated %.2f GiB, reserved %.2f GiB",
                    state.global_step,
                    torch.cuda.memory_allocated() / 2**30,
                    torch.cuda.memory_reserved() / 2**30)

    args.out.mkdir(parents=True, exist_ok=True)
    trainer = ChunkedLossTrainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(args.out),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch,
            # MEASURED (v0.6 pod run, 2026-07-30): HF's eval batch
            # defaults to 8 regardless of train batch. At 10240 tokens
            # that is a ~40 GiB logits allocation — the first eval
            # OOM'd a 14B run at epoch 1 and, because evaluation runs
            # BEFORE the epoch save, took 4.7 h of training with it.
            # Eval must never be heavier than a training step.
            per_device_eval_batch_size=1,
            prediction_loss_only=True,
            gradient_accumulation_steps=args.accum,
            learning_rate=args.lr,
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            logging_steps=10,
            eval_strategy="epoch",
            # Save on steps, not epochs: an epoch-end crash otherwise
            # loses the whole epoch (see above — it did).
            save_strategy="steps",
            save_steps=80,
            save_total_limit=2,
            bf16=True,
            report_to=[],
            seed=args.seed,
        ),
        train_dataset=ds_train,
        eval_dataset=ds_eval,
        # pad_to_multiple_of quantises batch shapes to a handful of
        # sizes. MEASURED (vram_probe, 2026-07-29): one full step at
        # 2048 peaks at 3.40 GiB — yet live runs climbed to 15.6 GiB
        # and died with driver-level OOMs. The difference is the
        # allocator: ~990 distinct sequence lengths each reserve new
        # cache blocks until the pool fragments beyond recovery.
        # Four shapes instead of a thousand costs ~15% pad compute
        # (labels on pads are masked) and removes the failure mode.
        data_collator=DataCollatorForLanguageModeling(
            tok, mlm=False, pad_to_multiple_of=512),
        callbacks=[EmptyCacheCallback()],
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
