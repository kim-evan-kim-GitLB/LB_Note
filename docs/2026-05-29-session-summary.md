# lb-note 세션 요약 — 2026-05-29

> 이 세션에서 진행한 작업과 의사결정을 시간순으로 정리한 로그.
> 기술 상세는 [`2026-05-29-slice-crash-and-repetition-fix.md`](./2026-05-29-slice-crash-and-repetition-fix.md) 참조.

---

## 세션 목표

기존 60s pipeline transcript 의 품질 이슈(반복)를 출발점으로, 10분 슬라이스 모드의 CUDA 크래시를
해결하고, 반복 hallucination 을 제거하며, 코드를 버전 관리 체계로 정리.

---

## 진행 순서 (시간순)

1. **품질 이슈 진단**: 사용자가 transcript 의 단일 문자 반복(`아아아`)을 지적 → 정량 분석 결과
   토큰의 **32~36% 가 반복 garbage**(`네x256`, `보호소x85`)임을 확인. 원인 = greedy 디코더의 무음 구간 루프.

2. **파이프라인 구조 설명**: `run.py → audio_io → chunker → cohere.generate → decode → merge` 흐름과
   3가지 처리 모드(단일/60s pipeline/10분 슬라이스)를 코드 기준으로 정리.

3. **우선순위 결정**: 사용자 통찰("청크 잘게 쪼갤수록 성능↓라 10분으로 갔다")에 따라
   **10분 슬라이스 크래시 해결을 1순위**로 채택. (반복도 청크 수와 연관된 공통 뿌리로 가설)

4. **크래시 근본 원인 규명**:
   - 기존 "SDPA mask invariant" 진단은 **오진**(async CUDA 에러의 가짜 위치).
   - `CUDA_LAUNCH_BLOCKING=1` 로 진짜 위치 확보 → **디코더 position embedding(1024행) 인덱스 초과**.
   - 반복 루프 청크가 `max_new_tokens=4096` 동안 1024 토큰을 넘겨 발생.
   - **fix**: `MAX_NEW_TOKENS 4096→1000`. m4a **9/9 통과** 검증.

5. **반복 hallucination 제거**:
   - `repetition_penalty=1.2` 채택 (A/B: 최악 구간 97%→0%, 정상 발화 보존).
   - **1.2 vs 1.3 단독 비교** → 1.2 가 이미 0% 달성, 1.3 은 distortion 만 증가 → **1.2 확정**.
   - 3개 generate 호출 전부 적용.

6. **공정 비교 (모드 × rp 매트릭스)**:

   | 모드 | rp 없음 (WER/반복) | rp1.2 (WER/반복) |
   |---|---|---|
   | 60s pipeline | 1.073 / 36.0% | 0.652 / 3.0% |
   | **10분 슬라이스** | 0.890 / 28.7% | **0.529 / 0.0%** |

   → 같은 rp1.2 조건에서도 10분 슬라이스 우위 = 청크 경계 적을수록 좋다는 **구조적 이점 확증**.

7. **버전 관리 정리**: lb-note 를 **독립 git 레포**로 init, README/데이터 정책 수립 (아래 참조).

---

## 주요 의사결정 로그

| 결정 | 내용 | 근거 |
|---|---|---|
| 크래시 수정 방식 | encoder 패딩(오진) 폐기 → `MAX_NEW_TOKENS=1000` | 진짜 원인이 pos_emb 1024 초과 |
| repetition_penalty 값 | **1.2** (1.3 불필요) | 1.2 가 이미 반복 0%, 1.3 은 distortion만↑. 표준범위 1.1~1.3 의 보수값 |
| git 구조 | lb-note **독립 레포** | STT 프로젝트 분리 관리 (4090 이전·공유 용이) |
| 대용량/데이터 | `models/`·`samples/`(음성) git 제외 → **Google Drive** | 대용량 + 음성=개인정보/기밀 |
| 회의 transcript | `answer/ax_tf_클로바.txt` 만 추적 포함 | 소유자 판단 (평가 reference 필요) |
| 외부 배포 | GitHub **private**, 단 **VAD 완료 후 push** | 코드만 공유, 데이터는 Drive |
| feat/vad 조율 | `[10분청킹+rp1.2]` 커밋(`677c4ff`) 위에서 분기 | 베이스 정합 + 깨끗한 출력 위에서 VAD 검증 |

---

## 부가 논의

- **4090 이전 시**: VRAM 제약(슬라이싱 필요·속도)은 대부분 해소되나, **pos_emb 1024 한계와 반복은
  GPU 무관한 모델 구조 문제**라 cap·rp 는 그대로 필요. 24GB면 ≤2시간 single-call 도 가능.
- **git vs Docker**: git=소스/이력/협업, Docker=런타임 산출물. **git 먼저, Docker(=git의 산출물) 나중**.

---

## 최종 상태

git (lb-note, 로컬 `main` — push 보류):
```
a8dd0f4 docs: 60s pipeline rp1.2 공정 비교
fe3d7c2 docs: 작업 정리 종합
7c9d0d7 docs: rp1.2 검증 결과
677c4ff feat: repetition_penalty=1.2   ← feat/vad 분기 base
8496367 feat: 초기 커밋 (10분 청킹 fix)
```

핵심 성과: 크래시 해결(9/9) + 반복 0% + WER 0.890→0.529 + 속도 RTFx 1.44→2.75.

---

## 남은 작업

- ⏸️ **feat/vad 분기** (다른 세션): base `677c4ff` 준비 완료.
- ⏸️ **GitHub private push**: VAD 구현 완료 후.
- ⏸️ **Dockerfile**: CUDA12.1 base. 미결정 — 모델 bake vs 볼륨 / 앱코드 bake vs 마운트.
- (선택) 합성 wav 회귀(13/13 + WER 0.054 유지) 재확인.
