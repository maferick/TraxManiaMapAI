"""Sample the fine-tuned LM and judge the output honestly.

The prototype's claim is narrow, so the evaluation is too. Four
questions, in order of how easy they are to fake:

1. **Parse rate.** Does the output parse as construction tokens at all?
   Strict parser, no repair. This is the floor: unparseable output is
   not a map, however plausible it looks.
2. **Degeneracy.** Distinct block types and top-block share, the same
   §8 metrics the corpus and walker are measured by. A model that emits
   ``P RoadTechStraight`` five hundred times parses perfectly and is
   worthless, so parse rate alone must never be reported as success.
3. **Vocabulary validity.** Are the block names real catalogue blocks?
   An invented name cannot be placed.
4. **Novelty.** Are sampled sequences copies of training sequences?
   Reported as longest common token run against the training set, so
   memorisation is visible rather than assumed away.

Compared throughout against the CORPUS's own numbers, and against the
base model with no adapter, so the fine-tune's contribution is
separable from what the pretrained model already did.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "construction_text",
    Path(__file__).resolve().parents[1] / "src" / "learning"
    / "construction_text.py")
_ct = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_ct)
ParseError, parse = _ct.ParseError, _ct.parse

_LOG = logging.getLogger("sample-lm")


def parse_prefix(text):
    """Parse the longest valid token prefix. Returns (tokens, status).

    A generation that hits the token cap is TRUNCATED, not invalid: the
    harness stopped it mid-token, the model did not emit garbage.
    Counting those as parse failures would understate the model exactly
    as much as repairing real garbage would overstate it, so the two are
    separated and reported separately.
    """
    fields = text.strip().split()
    i = 1 if fields and fields[0].startswith("#len=") else 0
    # Leading fragment: prompt/tokenizer desync, not model output.
    lead = 0
    while i < len(fields) and fields[i] not in ("P", "F", "V", "J", "G"):
        i += 1
        lead += 1
    body = fields[i:]
    # Longest prefix that parses cleanly.
    best, best_n = None, 0
    lo, hi = 0, len(body)
    for n in range(len(body), 0, -1):
        try:
            best = parse(" ".join(body[:n]))
            best_n = n
            break
        except ParseError:
            continue
    if best is None:
        return None, "invalid", lead
    status = "complete" if best_n == len(body) else "truncated"
    return best, status, lead


def metrics(token_lists, catalogue_names, train_grams):
    parsed = [t for t in token_lists if t is not None]
    out = {
        "sampled": len(token_lists),
        "parsed": len(parsed),
        "parse_rate": round(len(parsed) / max(1, len(token_lists)), 3),
    }
    if not parsed:
        return out

    distinct, shares, lens, unknown, ops = [], [], [], 0, collections.Counter()
    total_places = 0
    max_overlap = 0
    for tokens in parsed:
        blocks = [t["block"] for t in tokens if t["op"] == "PLACE"]
        for t in tokens:
            ops[t["op"]] += 1
        lens.append(len(tokens))
        if blocks:
            c = collections.Counter(blocks)
            distinct.append(len(c))
            shares.append(max(c.values()) / len(blocks))
            total_places += len(blocks)
            unknown += sum(1 for b in blocks if b not in catalogue_names)
        # Longest run of consecutive PLACE block names also in training.
        run = best = 0
        for i in range(len(blocks)):
            gram = tuple(blocks[max(0, i - 7):i + 1])
            if len(gram) >= 4 and gram in train_grams:
                run = len(gram)
                best = max(best, run)
        max_overlap = max(max_overlap, best)

    out.update({
        "mean_tokens": round(sum(lens) / len(lens), 1),
        "mean_places": round(total_places / len(parsed), 1),
        "distinct_blocks_mean": round(sum(distinct) / max(1, len(distinct)), 1),
        "top_block_share_mean": round(sum(shares) / max(1, len(shares)), 3),
        "unknown_block_rate": round(unknown / max(1, total_places), 3),
        "ops": dict(ops),
        "longest_training_ngram_echo": max_overlap,
    })
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapter", type=Path,
                    default=Path("data/artifacts/models/construction-lm-v0/final"))
    ap.add_argument("--base", type=str, default="Qwen/Qwen2.5-0.5B")
    ap.add_argument("--corpus", type=Path,
                    default=Path("data/artifacts/telemetry/construction_sequences_v0.2.jsonl"))
    ap.add_argument("--catalogue", type=Path,
                    default=Path("data/catalogue2/catalogue.ndjson"))
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--max-new", type=int, default=1400)
    ap.add_argument("--temperature", type=float, default=0.9)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--prompt", type=str, default="#len=med")
    ap.add_argument("--baseline", action="store_true",
                    help="sample the BASE model with no adapter")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from src.catalogue.loader import load_catalogue
    catalogue_names = set(load_catalogue(args.catalogue,
                                         collection="Stadium2020"))

    # Training n-grams for the memorisation check.
    train_grams = set()
    corpus_blocks, corpus_distinct, corpus_share = [], [], []
    for line in args.corpus.open(encoding="utf-8"):
        toks = json.loads(line)["tokens"]
        blocks = [t["block"] for t in toks if t["op"] == "PLACE"]
        for i in range(len(blocks)):
            for w in range(4, 9):
                if i + w <= len(blocks):
                    train_grams.add(tuple(blocks[i:i + w]))
        if blocks:
            c = collections.Counter(blocks)
            corpus_distinct.append(len(c))
            corpus_share.append(max(c.values()) / len(blocks))
            corpus_blocks.append(len(blocks))

    src = args.base if args.baseline else str(args.adapter)
    tok = AutoTokenizer.from_pretrained(
        args.base if args.baseline else str(args.adapter))
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.base, torch_dtype=torch.bfloat16, device_map="cuda:0")
    if not args.baseline:
        model = PeftModel.from_pretrained(model, str(args.adapter))
    model.eval()

    texts = []
    enc = tok(args.prompt, return_tensors="pt").to("cuda:0")
    for _ in range(args.n):
        with torch.no_grad():
            gen = model.generate(
                **enc, do_sample=True, temperature=args.temperature,
                top_p=args.top_p, max_new_tokens=args.max_new,
                pad_token_id=tok.pad_token_id,
            )
        texts.append(tok.decode(gen[0], skip_special_tokens=True))

    token_lists, status_counts = [], collections.Counter()
    leads = 0
    for t in texts:
        toks, status, lead = parse_prefix(t)
        token_lists.append(toks)
        status_counts[status] += 1
        leads += 1 if lead else 0

    m = metrics(token_lists, catalogue_names, train_grams)
    m["status"] = dict(status_counts)
    m["leading_fragment_rate"] = round(leads / max(1, len(texts)), 3)
    m["source"] = "base (no adapter)" if args.baseline else "fine-tuned"
    m["corpus_distinct_blocks_mean"] = round(
        sum(corpus_distinct) / len(corpus_distinct), 1)
    m["corpus_top_block_share_mean"] = round(
        sum(corpus_share) / len(corpus_share), 3)
    m["corpus_mean_places"] = round(sum(corpus_blocks) / len(corpus_blocks), 1)

    print(json.dumps(m, indent=1))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(
            {"metrics": m, "samples": texts[:5]}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
