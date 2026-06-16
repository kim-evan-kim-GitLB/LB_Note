"""정제 스테이지 패키지 (설계 §5·§9).

stage-pluggable: clean → (이후) summarize → agenda → action_items 가 같은 Stage 계약·
같은 어댑터·검증을 재사용한다. 추가 = stage 한 개.
"""
from __future__ import annotations
