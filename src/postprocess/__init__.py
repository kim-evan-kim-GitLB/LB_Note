"""LLM-무관 회의록 후처리 파이프라인 (Phase 1-a: 정제).

설계: docs/2026-06-04-postprocess-pipeline-design.md

구성:
- schema      : 정제 출력 스키마(CleanedSegment / CleanResult)
- glossary    : [A] 결정적 용어 교정(LLM 아님, 100% 재현)
- backends    : [B] LLMBackend 어댑터(교체 지점) + 레지스트리
- stages      : [C] 정제 스테이지(향후 summarize/agenda/action 추가 가능)
- validate    : [D] 스키마·편집비율·내용보존 게이트 + 리페어 루프
- pipeline    : [A]→[B]→[C]→[D] 오케스트레이션
"""
from __future__ import annotations
