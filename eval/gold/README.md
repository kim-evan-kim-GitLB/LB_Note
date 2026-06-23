# 멀티도메인 액션아이템 정답셋 (E 안건, 2026-06-17)

회의 유형이 개발 내부(axfull/axenh)에만 묶여 S2~S5(벤더·비개발 실무·경영·CS)에서
무슨 과추출·오귀속이 나도 회귀가 침묵하던 문제를 푼다(결정문서
`docs/2026-06-17-action-item-methodology-upgrade.md` §E).

- 스키마는 `eval/gold_actionitems.json` 과 동일: `items[]`(positives) + `negatives[]`.
  채점은 `src/postprocess/score_extraction.py` 의 `score()`/`score_precision()` 재사용,
  로드는 `load_gold_dir("eval/gold")`.
- **키워드는 도메인 전문용어가 아니라 행동·소관 토큰 위주**로 작성한다(개발 편향 재발 방지).

## 상태: SEED (착수 골격)

아래 파일들은 **실제 도메인 transcript 가 없는 시드**다. 회의 유형별 합격선(≥0.8 잠정)과
키워드 변별력은 실제 벤더/비개발/경영/CS 음원·추출 산출을 확보한 뒤 1차 측정으로 확정한다
(그 전까지 CI 는 well-formedness 만 검증, 회수율/정밀도 하드락은 걸지 않는다).

- `gold_s2_vendor.json` — 외부 업체/협력사 미팅
- `gold_s3_ops.json` — 비개발 실무팀(영업·총무·구매·HR)
- `gold_s4_exec.json` — 경영/임원 보고·의사결정
- `gold_s5_cs.json` — 고객 대면/CS 이슈
