"""WER/CER 계산 + 다양한 reference 포맷 자동 감지.

lb-note-archive/score.py 의 함수를 모듈화. 표준 라이브러리만 사용.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

PUNCT_RE = re.compile(r"[\.\,\!\?\…\"‘’“”]+")
SLASH_VARIANT_RE = re.compile(r"\(([^()/]+)\)/\(([^()/]+)\)")
WS_RE = re.compile(r"\s+")
# 클로바 노트 화자 헤더 행 (예: "참석자 1 00:11" / "참석자 12 1:02:33") — 평가 시 제거
CLOVA_SPEAKER_HEADER_RE = re.compile(r"^참석자\s+\d+\s+\d{1,2}:\d{2}(?::\d{2})?\s*$")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = PUNCT_RE.sub(" ", text)
    text = WS_RE.sub(" ", text).strip()
    return text


def levenshtein(ref: list, hyp: list) -> int:
    n, m = len(ref), len(hyp)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[m]


def wer(ref: str, hyp: str) -> float:
    ref_t = ref.split() if ref else []
    hyp_t = hyp.split() if hyp else []
    if not ref_t:
        return 0.0 if not hyp_t else 1.0
    return levenshtein(ref_t, hyp_t) / len(ref_t)


def cer(ref: str, hyp: str) -> float:
    ref_c = list(ref.replace(" ", ""))
    hyp_c = list(hyp.replace(" ", ""))
    if not ref_c:
        return 0.0 if not hyp_c else 1.0
    return levenshtein(ref_c, hyp_c) / len(ref_c)


def _reference_variants(speakertext: str) -> list[str]:
    matches = list(SLASH_VARIANT_RE.finditer(speakertext))
    if not matches:
        return [normalize(speakertext)]
    variants = [speakertext, speakertext]
    for m in matches:
        a, b = m.group(1), m.group(2)
        variants[0] = variants[0].replace(m.group(0), a, 1)
        variants[1] = variants[1].replace(m.group(0), b, 1)
    return [normalize(v) for v in variants]


def _build_reference_candidates(turns: list[dict]) -> list[str]:
    """ko_office_answer 호환 Dialogs 리스트에서 슬래시 변형 조합 candidates 생성."""
    per_turn = [_reference_variants(t["Speakertext"]) for t in turns]
    canonical = " ".join(v[0] for v in per_turn)
    candidates = [canonical]
    for idx, variants in enumerate(per_turn):
        if len(variants) > 1:
            alt = list(per_turn)
            alt[idx] = [variants[1]]
            candidates.append(" ".join(v[0] for v in alt))
    return candidates


def _best_match(candidates: list[str], hyp: str) -> tuple[str, float, float]:
    best = min(candidates, key=lambda r: wer(r, hyp))
    return best, wer(best, hyp), cer(best, hyp)


def load_reference_text(reference_path: Path) -> tuple[str, str]:
    """reference 파일을 읽어 (정규화된 단일 텍스트, 소스 종류) 반환.

    지원: AI Hub answer.json (Dialogs[*].Speakertext) / 클로바 노트 JSON / plain .txt
    """
    if reference_path.suffix.lower() == ".txt":
        lines = [
            ln.strip()
            for ln in reference_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        body = [ln for ln in lines if not CLOVA_SPEAKER_HEADER_RE.match(ln)]
        # 화자 헤더가 하나라도 제거됐으면 클로바 노트 txt 로 본다(CLAUDE.md: 헤더 제거 규칙).
        if len(body) < len(lines):
            return normalize(" ".join(body)), "clova_note_txt"
        return normalize(" ".join(body)), "plain_txt"

    data = json.loads(reference_path.read_text(encoding="utf-8"))

    if isinstance(data, dict) and "Dialogs" in data:
        turns = sorted(data["Dialogs"], key=lambda d: d.get("DialogNum", 0))
        return " ".join(normalize(t["Speakertext"]) for t in turns), "ai_hub"

    if isinstance(data, dict) and "segments" in data and isinstance(data["segments"], list):
        return " ".join(normalize(s.get("text", "")) for s in data["segments"]), "clova_note"

    if isinstance(data, list):
        return " ".join(normalize(s.get("text", "")) for s in data), "clova_note"

    raise ValueError(f"알 수 없는 reference 스키마: {reference_path}")


def evaluate(hypothesis: str, reference_path: Path) -> dict:
    """hypothesis 텍스트와 reference 파일로 WER/CER 계산.

    AI Hub 스키마면 슬래시 변형 best-match 까지 수행.
    """
    hyp = normalize(hypothesis)

    if reference_path.suffix.lower() == ".json":
        data = json.loads(reference_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "Dialogs" in data:
            candidates = _build_reference_candidates(
                sorted(data["Dialogs"], key=lambda d: d.get("DialogNum", 0))
            )
            _ref, w, c = _best_match(candidates, hyp)
            return {
                "reference_path": str(reference_path),
                "ref_source": "ai_hub",
                "wer": round(w, 4),
                "cer": round(c, 4),
            }

    ref, source = load_reference_text(reference_path)
    return {
        "reference_path": str(reference_path),
        "ref_source": source,
        "wer": round(wer(ref, hyp), 4),
        "cer": round(cer(ref, hyp), 4),
    }
