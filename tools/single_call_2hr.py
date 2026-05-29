"""단일 호출 모드(processor 내부 audio_chunk_index 활용)로 2시간 wav 처리 — 비교용.

기존 transcribe(path) 흐름을 그대로 따르되 max_new_tokens 만 8192 로 임시 상향.
파이프라인 청크 모드와 elapsed / WER / VRAM 비교에 사용.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import librosa  # noqa: E402
import torch  # noqa: E402

from src import config, scoring  # noqa: E402
from src.stt import get_backend  # noqa: E402


def main() -> int:
    audio = Path("samples/long_synth_120m.wav")
    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)

    backend = get_backend("cohere")
    backend.load()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    t0 = time.perf_counter()
    wav, _ = librosa.load(str(audio), sr=16000, mono=True)
    load_t = time.perf_counter() - t0
    print(f"[single] librosa.load: {load_t:.1f}s, samples={len(wav)}")

    inputs = backend._processor(wav, sampling_rate=16000, return_tensors="pt", language="ko")
    aci = inputs.get("audio_chunk_index")
    if aci is None:
        aci_info = None
    elif isinstance(aci, list):
        aci_info = f"list(len={len(aci)})"
    elif hasattr(aci, "shape"):
        aci_info = f"tensor{tuple(aci.shape)}"
    else:
        aci_info = type(aci).__name__
    print(f"[single] audio_chunk_index: {aci_info}")
    inputs = inputs.to(backend._model.device, dtype=backend._model.dtype)

    gen_t0 = time.perf_counter()
    with torch.inference_mode():
        outputs = backend._model.generate(**inputs, max_new_tokens=8192)
    gen_t = time.perf_counter() - gen_t0

    text = backend._processor.decode(
        outputs, skip_special_tokens=True, audio_chunk_index=aci, language="ko"
    )
    if isinstance(text, list):
        text = text[0] if text else ""
    text = text.strip()

    elapsed = time.perf_counter() - t0
    vram = torch.cuda.max_memory_allocated() // (1024 * 1024) if torch.cuda.is_available() else None
    backend.unload()

    print(f"[single] elapsed={elapsed:.1f}s (load={load_t:.1f}s, generate={gen_t:.1f}s)")
    print(f"[single] vram_peak={vram} MB, text_len={len(text)}")

    ref_data = json.loads(
        Path("/home/evan/Claude_workspace/lb-note-archive/samples/ko_office_answer.json")
        .read_text(encoding="utf-8")
    )
    turns = sorted(ref_data["Dialogs"], key=lambda x: x["DialogNum"])
    single_ref = " ".join(scoring.normalize(t["Speakertext"]) for t in turns)
    ref_40x = " ".join([single_ref] * 40)
    hyp = scoring.normalize(text)
    wer = scoring.wer(ref_40x, hyp)
    cer = scoring.cer(ref_40x, hyp)
    print(f"[single] WER={wer:.4f}, CER={cer:.4f}")

    duration = len(wav) / 16000.0
    payload = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "single_call_audio_chunk_index",
        "audio": {
            "source_path": str(audio),
            "duration_seconds": round(duration, 2),
            "sample_rate_normalized": 16000,
            "channels_normalized": 1,
        },
        "model": {
            "backend": "cohere",
            "name": Path(str(config.COHERE_MODEL_PATH)).name,
            "quantization": getattr(backend, "quantization", "") or "bf16",
            "max_new_tokens": 8192,
        },
        "pipeline": {
            "mode": "single_call",
            "audio_chunk_index": aci_info,
        },
        "performance": {
            "elapsed_seconds": round(elapsed, 2),
            "librosa_load_seconds": round(load_t, 2),
            "generate_seconds": round(gen_t, 2),
            "rtfx": round(duration / elapsed, 2) if elapsed > 0 else None,
            "vram_peak_mb": vram,
        },
        "evaluation": {
            "reference_path": "/home/evan/Claude_workspace/lb-note-archive/samples/ko_office_answer.json (×40 repeated)",
            "ref_source": "synthetic_40x_ai_hub",
            "wer": round(wer, 4),
            "cer": round(cer, 4),
        },
        "transcript": text,
    }
    out_json = out_dir / "text-single_call_120m.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[single] saved: {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
