# RunPod training runbook

Train the construction LM on a rented GPU instead of the local
4070 Ti SUPER (16 GB), which caps the base model at ~1.5B and the
corpus at ~700-block maps. Written 2026-07-30 after the v0.5 run
established the raw-map corpus direction in-game (14/18 = 78%
structurally valid vs v0.4's 45%).

The division of labour does not change: the pod TRAINS and SAMPLES;
the Windows machine with Trackmania REALISES and VALIDATES. Only two
files cross the wire in, one directory comes back.

## 1. What to rent

| GPU | base model | notes |
|---|---|---|
| 4090 24GB | Qwen/Qwen2.5-7B | budget option |
| L40S / A6000 48GB | Qwen/Qwen2.5-14B | recommended |
| A100/H100 80GB | 14B (fast) or 32B | 32B is an experiment |

Template: any recent PyTorch CUDA image (torch >= 2.4). Disk: 60 GB+
(model shards + corpus + checkpoints).

## 2. Ship the inputs (from the Windows machine)

Only the trainer and the corpus are needed — no repo clone, no DB:

```bash
scp tools/train_construction_lm.py root@<POD_IP>:/workspace/
scp data/artifacts/telemetry/raw_map_sequences_v0.2.jsonl root@<POD_IP>:/workspace/
scp tools/sample_construction_lm.py src/learning/construction_text.py root@<POD_IP>:/workspace/
```

(`sample_construction_lm.py` imports `construction_text` via the repo
layout; on the pod, drop both in /workspace and add
`sys.path`/copy as needed — it has no other repo dependencies.)

## 3. Pod setup

```bash
pip install -U transformers peft datasets accelerate
```

## 4. Train

The trainer already carries every hard-won GPU lesson from the local
runs and they all still apply on big cards (comments in the file,
marked MEASURED): chunked checkpointed cross-entropy (the stock loss
materialises fp32 logits for the full sequence — at 16k tokens x 152k
vocab that is 10+ GiB even on an A100), `pad_to_multiple_of` shape
quantisation (allocator fragmentation from ~10k distinct sequence
lengths), `--drop-overlong` (NEVER truncate: a truncated map loses its
Finish, which was the measured corpus defect).

48 GB card, 14B:

```bash
python train_construction_lm.py \
  --corpus raw_map_sequences_v0.2.jsonl \
  --model Qwen/Qwen2.5-14B \
  --out construction-lm-v06 \
  --max-len 16384 --drop-overlong \
  --batch 1 --accum 16 --epochs 3
```

24 GB card: same but `--model Qwen/Qwen2.5-7B --max-len 8192`.

Rough wall-clock at 48 GB / 14B: 4-8 h for 3 epochs over ~12k maps.
Watch the first 50 steps: the `vram step N` telemetry lines print
allocated vs reserved; if reserved runs away from allocated the
allocator is fragmenting and the run will crawl — that is the failure
mode `pad_to_multiple_of` exists to prevent, so it appearing anyway
means something changed.

## 5. Sample on the pod

```bash
python sample_construction_lm.py \
  --adapter construction-lm-v06/final \
  --base Qwen/Qwen2.5-14B \
  --n 24 --max-new 14000 \
  --out samples_v06.json
```

`--max-new` must cover the longest trained sequences or the sampler
truncates generations mid-map and the missing-Finish defect reappears
as a harness artifact (measured on v0.5: 14/24 falsely truncated at
the default cap).

## 6. Bring back and judge locally

```bash
scp root@<POD_IP>:/workspace/samples_v06.json data/artifacts/models/construction-lm-v06/
scp -r root@<POD_IP>:/workspace/construction-lm-v06/final data/artifacts/models/construction-lm-v06/   # optional, for provenance
```

Then the standard local protocol (Trackmania open, TMMapControl
plugin loaded):

```bash
./.venv-train/Scripts/python.exe tools/realise_generated_map.py \
  --samples data/artifacts/models/construction-lm-v06/samples_v06.json \
  --out-dir "C:/Users/gijsv/Documents/Trackmania/Maps/AIGenV6" --limit 24
```

Compare against the recorded baselines: v0.4 10/22 = 45%,
v0.5 14/18 = 78% of grid-expressible maps Validable.

## 7. Known limits that a bigger model does NOT fix

Measured 2026-07-30, so nobody re-learns them the expensive way:

* **Connectivity and facing are encoding problems.** File-order
  training data is only 70.6% consecutive-adjacent, so "pieces that
  do not touch" is IN the data; and joins require summing deltas
  across tokens. The fix is route-first reordering of the corpus (or
  constrained decoding), not parameters.
* **Scenery**: visible TM2020 scenery is mostly free-placed ITEMS,
  which the exporter excludes (grid-only). Grid-block scenery share
  tracks the length bucket (small maps 20%, large 42%) — sample with
  `#len=long` to see it.
* Kill the pod when done. Checkpoints are also saved per-epoch under
  `--out`, so a preempted spot instance loses at most one epoch.
