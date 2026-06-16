"""후처리 파이프라인 CLI 진입점 (STT run.py 와 분리, 설계 §10).

모드(MODE) — 정제를 어떻게 수행할지:
  auto (기본)  : 기존 backend 경로([A]glossary→[C]stage(backend)→[D]게이트→산출).
                 passthrough/스텁 백엔드로 배선 검증. 모든 선행 수정(F1–F8) 유지.
  emit         : 인-세션 핸드오프 1단계. text.json → glossary 교정 → work-order(JSON+MD) 발행.
                 이 세션의 코딩 에이전트(Claude Code/Codex)가 work-order 의 cleaned 를 채운다.
  collect      : 인-세션 핸드오프 2단계. 채워진 work-order → [D]게이트 → 정상 산출.

사용:
  # auto (기존 경로, 하위호환)
  python run_postprocess.py output/text-meeting.json --backend passthrough --out output/pp
  python run_postprocess.py --mode auto output/text-meeting.json --backend passthrough

  # 핸드오프 2-phase
  python run_postprocess.py emit output/text-meeting.json --out output/pp
  #   (에이전트가 work-order 의 cleaned 채움)
  python run_postprocess.py collect output/pp/text-meeting.workorder.json --out output/pp

클라우드 백엔드(openai/anthropic)는 온프렘/PII 제약상 평가·벤치마크 전용이며,
--allow-cloud 없이는 동작하지 않는다(설계 §4 보안/PII 경계).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.postprocess.handoff import collect_workorder, emit_workorder
from src.postprocess.pipeline import run_postprocess

CLOUD_BACKENDS = {"openai", "anthropic"}
MODES = {"auto", "emit", "collect"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM-무관 회의록 후처리(정제) 파이프라인")
    ap.add_argument(
        "--mode", choices=sorted(MODES), default=None,
        help="실행 모드 (auto[기본] | emit | collect). 첫 위치인자로도 줄 수 있음.",
    )
    ap.add_argument(
        "input", type=str,
        help="auto/emit: STT 산출 text-{stem}.json | collect: work-order JSON "
             "(또는 첫 위치인자가 emit/collect/auto 면 모드로 해석).",
    )
    ap.add_argument("rest", nargs="?", default=None,
                    help="모드를 위치인자로 줬을 때의 입력 경로.")
    ap.add_argument(
        "--backend", default="passthrough",
        help="auto 모드 LLM 백엔드 (passthrough[기본] | local_vllm | ollama | "
             "openai | anthropic | agent_cli[STUB]).",
    )
    ap.add_argument("--out", type=Path, default=Path("output/postprocess"),
                    help="산출 디렉터리(기본: output/postprocess)")
    ap.add_argument("--glossary", type=Path, default=None,
                    help="glossary JSON 경로(기본: config/glossary.ko.json)")
    ap.add_argument("--edit-lo", type=float, default=0.0, help="편집비율 밴드 하한")
    ap.add_argument("--edit-hi", type=float, default=0.6, help="편집비율 밴드 상한")
    ap.add_argument("--group-chars", type=int, default=0,
                    help="(auto) 인접 짧은 segment 묶음 글자수 예산(0=묶음 OFF). 출력은 항상 1:1 유지.")
    ap.add_argument("--require-edit", action="store_true",
                    help="(collect) 무편집 segment 를 '정제 실패'로 게이트에서 잡음(기본 OFF).")
    ap.add_argument("--allow-cloud", action="store_true",
                    help="클라우드 백엔드(openai/anthropic) 허용. PII 외부전송 동의 시에만.")
    ap.add_argument("--no-overwrite", action="store_true",
                    help="기존 산출이 있으면 덮어쓰지 않고 종료(멱등성).")
    args = ap.parse_args(argv)

    # 모드 해석: --mode 플래그 우선. 없으면 첫 위치인자가 모드 키워드인지 확인.
    mode = args.mode
    if args.input in MODES and args.mode is None:
        mode = args.input
        input_path = args.rest
    else:
        if mode is None:
            mode = "auto"
        input_path = args.input
        if args.rest is not None:
            print(f"예상치 못한 추가 인자: {args.rest}", file=sys.stderr)
            return 2

    if not input_path:
        print(f"입력 경로가 없습니다(mode={mode}).", file=sys.stderr)
        return 2
    input_path = Path(input_path)
    if not input_path.exists():
        print(f"입력 파일 없음: {input_path}", file=sys.stderr)
        return 2

    if mode == "emit":
        emit_workorder(
            text_json=input_path,
            out_dir=args.out,
            glossary_path=args.glossary,
        )
        return 0

    if mode == "collect":
        collect_workorder(
            workorder_json=input_path,
            out_dir=args.out,
            glossary_path=args.glossary,
            edit_lo=args.edit_lo,
            edit_hi=args.edit_hi,
            require_edit=args.require_edit,
            overwrite=not args.no_overwrite,
        )
        return 0

    # mode == "auto" — 기존 backend 경로(F1–F8 유지).
    # [F8] 클라우드 백엔드 게이트: 온프렘/PII 제약상 --allow-cloud 없이는 금지.
    if args.backend.strip().lower() in CLOUD_BACKENDS and not args.allow_cloud:
        print(
            f"클라우드 백엔드 '{args.backend}' 는 --allow-cloud 없이 사용할 수 없습니다. "
            "회의 내용은 온프렘 전제이며 클라우드 백엔드는 평가·벤치마크 전용입니다"
            "(설계 §4 보안/PII 경계). 외부 전송에 동의하면 --allow-cloud 를 명시하세요.",
            file=sys.stderr,
        )
        return 2

    run_postprocess(
        text_json=input_path,
        out_dir=args.out,
        backend=args.backend,
        glossary_path=args.glossary,
        edit_lo=args.edit_lo,
        edit_hi=args.edit_hi,
        group_chars=args.group_chars,
        overwrite=not args.no_overwrite,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
