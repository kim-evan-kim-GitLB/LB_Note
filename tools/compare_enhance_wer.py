"""노이즈 감소(combo) 전처리가 STT 정확도에 도움이 되는지 검증.

20-80s 슬라이스의 raw / wpe25 / combo 3종 STT 결과를 동일 reference 구간에
대해 WER/CER + repetition(collapse) 지표로 비교한다.

reference: answer/ax_tf_클로바.txt 에서 [start,end] 구간에 겹치는 화자 블록 추출.
  - 경계 블록(시작이 window 밖)은 부분 겹침이라 절대 WER 은 부풀 수 있으나,
    세 변종 모두 동일 reference 를 쓰므로 변종 간 Δ(개선 여부)는 공정하다.

사용 예:
    uv run python tools/compare_enhance_wer.py --start 20 --end 80
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scoring import cer, normalize, wer  # noqa: E402

REF_PATH = ROOT / "answer" / "ax_tf_클로바.txt"
HEADER_RE = re.compile(r"^참석자\s+(\d+)\s+(\d{1,2}):(\d{2})\s*$")
MIN_RUN = 5

VARIANTS = {
    "raw": ROOT / "output" / "stt_20-80s_raw.txt",
    "wpe25": ROOT / "output" / "stt_20-80s_wpe25.txt",
    "combo": ROOT / "output" / "stt_20-80s_combo.txt",
}


def parse_blocks(path: Path) -> list[tuple[int, str]]:
    """클로바 txt → [(start_sec, utterance_text), ...]."""
    blocks: list[tuple[int, str]] = []
    cur_start: int | None = None
    buf: list[str] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = HEADER_RE.match(ln.strip())
        if m:
            if cur_start is not None and buf:
                blocks.append((cur_start, " ".join(buf)))
            cur_start = int(m.group(2)) * 60 + int(m.group(3))
            buf = []
        elif ln.strip():
            buf.append(ln.strip())
    if cur_start is not None and buf:
        blocks.append((cur_start, " ".join(buf)))
    return blocks


def reference_window(start: float, end: float) -> tuple[str, list[int]]:
    """window [start,end] 에 겹치는 블록 텍스트 결합 + 사용된 블록 시작초 목록."""
    blocks = parse_blocks(REF_PATH)
    used = []
    texts = []
    for i, (s, text) in enumerate(blocks):
        nxt = blocks[i + 1][0] if i + 1 < len(blocks) else s + 60
        # 블록 발화 구간 [s, nxt) 이 window 와 겹치면 포함
        if s < end and nxt > start:
            used.append(s)
            texts.append(text)
    return normalize(" ".join(texts)), used


def repetition_ratio(tokens: list[str], min_run: int) -> tuple[float, int]:
    n = len(tokens)
    i = 0
    rep_tokens = 0
    bursts = 0
    while i < n:
        j = i
        while j < n and tokens[j] == tokens[i]:
            j += 1
        run = j - i
        if run >= min_run:
            rep_tokens += run
            bursts += 1
        i = j
    return (rep_tokens / n if n else 0.0), bursts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=20.0)
    ap.add_argument("--end", type=float, default=80.0)
    args = ap.parse_args()

    ref, used = reference_window(args.start, args.end)
    ref_tok = ref.split()
    print(f"reference window [{args.start:.0f},{args.end:.0f}]s → "
          f"블록 시작초={used}, ref tokens={len(ref_tok)}")
    print("=" * 78)
    print(f"{'variant':<8}{'WER↓':>9}{'CER↓':>9}{'hyp_tok':>9}"
          f"{'tok_ratio':>11}{'rep_ratio↓':>12}{'bursts↓':>9}")
    print("-" * 78)

    rows = []
    for name, path in VARIANTS.items():
        if not path.exists():
            print(f"{name:<8}  (출력 없음: {path.name})")
            continue
        hyp = normalize(path.read_text(encoding="utf-8"))
        hyp_tok = hyp.split()
        w, c = wer(ref, hyp), cer(ref, hyp)
        rr, nb = repetition_ratio(hyp_tok, MIN_RUN)
        ratio = len(hyp_tok) / len(ref_tok) if ref_tok else 0.0
        rows.append((name, w, c, len(hyp_tok), ratio, rr, nb))
        print(f"{name:<8}{w:>9.3f}{c:>9.3f}{len(hyp_tok):>9}"
              f"{ratio:>11.3f}{rr:>12.3f}{nb:>9}")
    print("=" * 78)

    if len(rows) >= 2 and rows[0][0] == "raw":
        base = rows[0]
        print("\nΔ vs raw (음수 = 개선):")
        for r in rows[1:]:
            print(f"  {r[0]:<8} WER {r[1]-base[1]:+.3f}  CER {r[2]-base[2]:+.3f}  "
                  f"rep_ratio {r[5]-base[5]:+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
