# Vendored: GTCRN

- **Upstream**: https://github.com/Xiaobin-Rong/gtcrn
- **Commit**: `3862c44808dca492ea5a8a145d2dc2a1028d08c8`
- **License**: MIT (see `LICENSE` in this directory) — Copyright (c) 2024 Rong Xiaobin
- **Vendored on**: 2026-05-29

## What is vendored
- `gtcrn.py` — model definition (unmodified). GTCRN: ShuffleNetV2 + SFE + TRA + 2 DPGRNN, ~23.67K params, 33 MMACs, 16kHz.
- `LICENSE` — upstream MIT license (retained per its terms).

## What is NOT vendored
- Pretrained checkpoint `model_trained_on_dns3.tar` lives in `models/gtcrn/` (gitignored — fetched per worktree, not committed).
- Streaming variant (`stream/`) — deferred; see `docs/denoiser-zipenhancer-plan.md`.

## Usage
Loaded via `src/backends/gtcrn_denoiser.py` (`GTCRNDenoiser`). Input/output contract: 16kHz mono float32 ndarray. STFT(512, hop 256, win=hann(512).pow(0.5)), per upstream `infer.py`.
