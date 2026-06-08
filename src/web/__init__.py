"""웹 서비스 레이어 — meetscript-ai 프론트엔드를 온프렘 파이프라인에 연결(FastAPI).

설계: /home/evan/.claude/plans/replicated-fluttering-squirrel.md (v1).
v1 범위: 온프렘 STT(transcript) + 결정적 glossary 교정 + SQLite 영속.
정제(LLM)·액션아이템·요약은 v2(로컬 LLM 백엔드)로 연기 — passthrough라 actionItems/summary는 빈 값.
"""
