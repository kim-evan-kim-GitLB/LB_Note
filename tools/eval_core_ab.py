"""다중 agent core A/B 평가 — 실제 LLM(agent_cli)으로 구버전 vs 신버전을 같은 입력에 돌린다.

A(구버전, PR#58 이전): summarize-ko-1.0 / extract-ko-1.4 프롬프트 + **순차 2콜**
                        (요약 1콜 -> 결정/이슈를 힌트로 -> 추출 1콜). 언어 게이트·critic 없음.
B(신버전): 다중 agent core — 언어 게이트 -> 라우팅 -> 창별 요약·추출 **병렬** -> (장시간이면 reduce)
           -> critic 1패스 -> 결정적 적용.

입력은 기존 STT 산출물(output/**/text-*.json)이라 GPU·STT 없이 **LLM 단계만** 비교한다.
같은 세그먼트, 같은 백엔드, 같은 순서로 돌리므로 차이는 프롬프트+오케스트레이션에서만 온다.

주의(공정성): claude CLI 는 temperature/seed 를 노출하지 않아 **비결정적**이다. 1회 실행 비교에는
런 간 변동이 섞인다 — --repeat 로 반복해 변동폭을 함께 봐야 한다.

실행:
  sudo PYTHONPATH=/app .venv/bin/python tools/eval_core_ab.py --dataset asr_test --version A
  sudo PYTHONPATH=/app .venv/bin/python tools/eval_core_ab.py --dataset asr_test --version B
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.postprocess.backends import get_llm_backend  # noqa: E402
from src.postprocess.extract_schema import seconds_to_timestamp  # noqa: E402
from src.postprocess.orchestrator import run_meeting_core  # noqa: E402
from src.postprocess.stages.extract import ExtractStage  # noqa: E402
from src.postprocess.stages.summarize import SummarizeStage  # noqa: E402
from src.postprocess.summarize_schema import ground_summary  # noqa: E402
from src.postprocess.web_contract import _action_items_from_payload  # noqa: E402

DATASETS = {
    "asr_test": "output/asr_test/text-asr test.json",
    "asr_test_wpe": "output/asr_test_wpe/text-asr test.json",
    "ax": "output/verify/text-ax과제회의(클로바노트)_음성파일.json",
}


def load_segments(rel: str) -> list[dict]:
    """STT 산출물 → core/스테이지 입력([{id,start,end,text}])."""
    data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
    out = []
    for i, s in enumerate(data.get("segments", [])):
        text = (s.get("cleaned") or s.get("text") or "").strip()
        if not text:
            continue
        # text-*.json 은 id 가 없다(정제 전 산출물) — 파이프라인의 normalize_segments 와 동일하게
        # **위치 인덱스**를 id 로 쓴다. A/B 양쪽이 같은 id 공간을 보므로 비교에 영향 없다.
        out.append(
            {
                "id": int(s["id"]) if s.get("id") is not None else i,
                "start": float(s.get("start") or 0.0),
                "end": float(s.get("end") or s.get("start") or 0.0),
                "text": text,
            }
        )
    return out


def _summary_hints(summary: dict | None) -> list[str]:
    """구버전 방법2 힌트: 요약의 결정·이슈 텍스트(중복 제거)."""
    hints: list[str] = []
    seen: set[str] = set()
    for block in (summary or {}).get("agenda") or []:
        for key in ("decisions", "issues"):
            for it in block.get(key) or []:
                t = str(it.get("text", "")).strip()
                if t and t not in seen:
                    seen.add(t)
                    hints.append(t)
    return hints


def run_legacy(segments: list[dict], backend_name: str, prompt_dir: Path) -> dict:
    """A: 구버전 순차 2콜 경로 재현(구 프롬프트 사용)."""
    backend = get_llm_backend(backend_name)
    calls = 0
    summary = SummarizeStage(prompt_dir / "summarize.ko.md").run(segments, backend)
    calls += 1
    summary = ground_summary(summary, segments)  # 저신뢰 인자 없음(구버전)
    summary_d = summary.to_dict()
    result = ExtractStage(prompt_dir / "extract.ko.md").run(
        segments, backend, ctx={"summary_hints": _summary_hints(summary_d)}
    )
    calls += 1
    start_by_id = {s["id"]: s["start"] for s in segments}
    for it in result.items:
        ev = [start_by_id[i] for i in it.evidence_seg_ids if i in start_by_id]
        it.anchor = seconds_to_timestamp(min(ev)) if ev else None
    actions = _action_items_from_payload(result.to_dict())
    return {"summary": summary_d, "actionItems": actions, "coreMeta": {"calls": {"total": calls}}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=sorted(DATASETS), default="asr_test")
    # C = 원인 분리용 대조군: **새 프롬프트 + 구 오케스트레이션(순차 2콜, 게이트·critic 없음)**.
    # A vs C = 프롬프트 효과, C vs B = 오케스트레이션(게이트·critic·병렬) 효과로 분해된다.
    ap.add_argument("--version", choices=["A", "B", "C"], required=True)
    ap.add_argument("--backend", default="agent_cli")
    ap.add_argument("--run", type=int, default=1, help="반복 실행 번호(파일명 구분·변동폭 측정용)")
    ap.add_argument(
        "--old-prompts",
        default=str(ROOT / "output" / "eval-core" / "old_prompts"),
        help="A 버전이 쓸 구 프롬프트 디렉토리(summarize.ko.md / extract.ko.md)",
    )
    args = ap.parse_args()

    segments = load_segments(DATASETS[args.dataset])
    out_dir = ROOT / "output" / "eval-core"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[eval] dataset={args.dataset} segments={len(segments)} version={args.version} run={args.run}")
    t0 = time.time()
    if args.version == "A":
        payload = run_legacy(segments, args.backend, Path(args.old_prompts))
    elif args.version == "C":
        payload = run_legacy(segments, args.backend, ROOT / "prompts")  # 새 프롬프트 + 구 경로
    else:
        payload = run_meeting_core(
            segments, summarize_backend=args.backend, extract_backend=args.backend
        )
    elapsed = time.time() - t0

    payload["_eval"] = {
        "dataset": args.dataset,
        "version": args.version,
        "run": args.run,
        "backend": args.backend,
        "segments": len(segments),
        "elapsedSec": round(elapsed, 1),
    }
    path = out_dir / f"{args.dataset}-{args.version}-run{args.run}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    n_agenda = len((payload.get("summary") or {}).get("agenda") or [])
    print(f"[eval] {elapsed:.1f}s  안건 {n_agenda}개  액션 {len(payload.get('actionItems') or [])}개 -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
