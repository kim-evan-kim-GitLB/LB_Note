"""합성 wav 의 실제 구조에 맞춘 정확한 reference 로 WER/CER 재계산.

합성 wav = [wav(182.09s) + gap(0.5s)] × N, 마지막은 target_seconds 에 맞춰 절단.
이전 평가는 reference 를 단순 ×N 으로 만들어 마지막 부분 절단을 무시했음.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import scoring  # noqa: E402

ANSWER_JSON = Path("/home/evan/Claude_workspace/lb-note-archive/samples/ko_office_answer.json")
SOURCE_WAV_DUR = 182.091
GAP_SEC = 0.5
UNIT = SOURCE_WAV_DUR + GAP_SEC


def build_partial_dialog_text(dialogs: list[dict], cutoff_sec: float) -> str:
    """cutoff_sec 시점까지 들어간 부분만 dialog 텍스트로 구성.

    각 Dialog 가 cutoff 안에 완전히 들어가면 그대로, 일부만 들어가면 발화 비율만큼 글자 절단.
    """
    parts = []
    for d in dialogs:
        start = float(d["StartTime"])
        end = float(d["EndTime"])
        text = scoring.normalize(d["Speakertext"])
        if not text:
            continue
        if end <= cutoff_sec:
            parts.append(text)
        elif start >= cutoff_sec:
            break
        else:
            ratio = (cutoff_sec - start) / (end - start)
            n_chars = max(0, int(round(len(text) * ratio)))
            if n_chars > 0:
                parts.append(text[:n_chars])
    return " ".join(parts)


def build_accurate_reference(target_seconds: float, dialogs: list[dict]) -> str:
    """target_seconds 길이의 합성 wav 에 들어간 텍스트를 정확히 재구성."""
    full_text = " ".join(scoring.normalize(d["Speakertext"]) for d in dialogs)
    n_full = int(target_seconds // UNIT)
    last_start = n_full * UNIT
    remainder = target_seconds - last_start
    last_wav_in = min(remainder, SOURCE_WAV_DUR)
    last_part = build_partial_dialog_text(dialogs, last_wav_in)
    return " ".join([full_text] * n_full + ([last_part] if last_part else []))


def rescore(text_json: Path, target_seconds: float) -> dict:
    d = json.loads(text_json.read_text(encoding="utf-8"))
    ref_data = json.loads(ANSWER_JSON.read_text(encoding="utf-8"))
    dialogs = sorted(ref_data["Dialogs"], key=lambda x: x["DialogNum"])

    ref_accurate = build_accurate_reference(target_seconds, dialogs)
    ref_naive = " ".join(
        [" ".join(scoring.normalize(t["Speakertext"]) for t in dialogs)]
        * int(round(target_seconds / SOURCE_WAV_DUR))
    )
    hyp = scoring.normalize(d["transcript"])

    return {
        "duration_seconds": target_seconds,
        "n_full_repeats": int(target_seconds // UNIT),
        "last_wav_in_seconds": round(target_seconds - int(target_seconds // UNIT) * UNIT, 2),
        "ref_naive_tokens": len(ref_naive.split()),
        "ref_accurate_tokens": len(ref_accurate.split()),
        "hyp_tokens": len(hyp.split()),
        "wer_naive": round(scoring.wer(ref_naive, hyp), 4),
        "wer_accurate": round(scoring.wer(ref_accurate, hyp), 4),
        "cer_naive": round(scoring.cer(ref_naive, hyp), 4),
        "cer_accurate": round(scoring.cer(ref_accurate, hyp), 4),
    }


def main() -> int:
    cases = [
        ("output/text-long_synth_10m.json", 600.0),
        ("output/text-long_synth_120m.json", 7200.0),
    ]
    rows = []
    for path, target in cases:
        p = Path(path)
        if not p.exists():
            print(f"skip (없음): {path}")
            continue
        r = rescore(p, target)
        r["file"] = p.name
        rows.append(r)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        print("---")

    out = Path("output/score-synth-rescore.md")
    lines = [
        "# 합성 wav 재평가 — timestamp 기반 정확 reference",
        "",
        "합성 wav 구조: `[source(182.091s) + gap(0.5s)] × N`, target 길이에 맞춰 끝 절단.",
        "이전 단순 ×N reference 는 마지막 절단을 무시함. timestamp 기반으로 마지막 부분 dialog 까지 정확히 반영해 재계산.",
        "",
        "| 합성 | duration | 전체반복 | 마지막반복 wav 길이 | WER 단순×N | **WER 정확** | CER 단순×N | **CER 정확** |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['file']} | {r['duration_seconds']:.0f}s | {r['n_full_repeats']} | "
            f"{r['last_wav_in_seconds']:.2f}s | {r['wer_naive']:.3f} | **{r['wer_accurate']:.3f}** | "
            f"{r['cer_naive']:.3f} | **{r['cer_accurate']:.3f}** |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[rescore] saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
