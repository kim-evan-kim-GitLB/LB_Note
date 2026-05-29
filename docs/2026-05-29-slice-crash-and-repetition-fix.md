# lb-note STT — 10분 슬라이스 크래시 & 반복 hallucination 해결 (2026-05-29)

> 한국어 회의 음성 STT 파이프라인(Cohere transcribe-03-2026)에서 (1) 10분 슬라이스 모드의
> CUDA 크래시와 (2) 반복 hallucination 을 진단·해결한 작업 기록.

---

## 0. 요약 (TL;DR)

| 항목 | 결과 |
|---|---|
| 10분 슬라이스 CUDA assert | ✅ 해결 (`MAX_NEW_TOKENS 4096→1000`), m4a 9/9 통과 |
| 반복 hallucination | ✅ 해결 (`repetition_penalty=1.2`), 반복 28.7%→0% |
| WER (클로바 ref) | 0.890 → **0.529** (-40%) |
| 처리 속도 | RTFx 1.44 → **2.75** (루프 조기 종료, ~2배) |
| 버전 관리 | lb-note 독립 git 레포 + README + 데이터 정책 수립 |

근본 원인 두 가지가 **같은 뿌리**였음: 무음/저정보 구간에서 디코더가 한 토큰에 갇히는 **반복 루프**.
짧으면(≤1024 토큰) 출력 오염만, 길면(>1024) position embedding 인덱스 초과로 **크래시**.

---

## 1. 시스템 개요

- 모델: `CohereLabs/cohere-transcribe-03-2026` (`CohereAsrForConditionalGeneration`, trust_remote_code)
- 환경: Python 3.12, torch 2.5.1+cu121, transformers 5.5.0, RTX 3050 Ti (VRAM 4GB, driver fallback)
- 처리 모드 3종:

| 모드 | 코드 | 외부 청크 | max_new_tokens |
|---|---|---|---|
| 단일 호출 | `run.py audio` | 없음 | 256 |
| 60s pipeline | `run.py --pipeline` | 60s/10s | 512 |
| 10분 슬라이스 | `tools/run_long_slice10m.py` | 600s/5s | 1000 (구 4096) |

### 파이프라인 데이터 흐름
```
load_audio (16kHz mono 정규화)
  → chunk/slice (외부 청크 분할)
  → [processor: 내부 ~30s energy-based 청킹 → audio_chunk_index]
  → model.generate (디코더, greedy)
  → processor.decode (학습된 머지)
  → merge_segments (overlap dedupe)
  → text.json / transcript.md
```
핵심: **generate 는 내부 30초 청크(batch) 단위로 독립 생성**한다. 정상 30초 발화는 수백 토큰.

---

## 2. 문제 1 — 10분 슬라이스 CUDA assert

### 증상
m4a(83분) 10분 슬라이스 처리 시 항상 2번째 슬라이스에서 device-side assert. 합성 wav(2시간)는 13/13 통과.

### 오진 (2026-05-28, 폐기)
"energy-based internal chunking → 슬라이스마다 encoder seq_len 변동 → SDPA mask invariant 위반"
로 진단하고 `masking_utils.py:326 padding_mask.all()` 을 원인 지점으로 봄.
→ **틀렸음.** encoder attention_mask 고정 패딩(`FIXED_ENCODER_LEN`)을 구현했으나 슬라이스 1은 여전히 크래시.

### 진짜 원인 (`CUDA_LAUNCH_BLOCKING=1` 로 확보)
CUDA 에러는 비동기라 보고된 위치가 가짜일 수 있다. `CUDA_LAUNCH_BLOCKING=1` 로 동기 실행하니
진짜 위치가 드러남:
```
modeling_cohere_asr.py:387  pos_emb = self.pos_emb(position_ids.squeeze(0))
RuntimeError: CUDA error: device-side assert triggered  (embedding index out-of-range)
```
- 디코더 position embedding 테이블 = `config.transf_decoder.max_sequence_length = 1024` 행
- `MAX_NEW_TOKENS=4096` 이라, 반복 루프에 빠진 청크가 토큰을 1024 넘게 생성 → 인덱스 초과
- `masking_utils.py:326 padding_mask.all()` 은 GPU→host 동기화(`.all()`) 지점이라 에러가 거기서 surface 됐을 뿐

### 모드별 분기
| 모드 | max_new_tokens | 루프 발생 시 |
|---|---|---|
| 60s pipeline | 512 (<1024) | 크래시 없이 반복 garbage 만 |
| 10분 슬라이스 | 4096 (>1024) | 1024 넘겨 **크래시** |
| 합성 wav | (균일 에너지) | 루프 자체가 없어 통과 (seq_len 변동은 무관) |

### 해결
`tools/run_10m_slice.py`: `MAX_NEW_TOKENS = 4096 → 1000` (1024 미만 cap).
오진 기반 encoder 패딩 + 효과 없던 per-slice model reload 는 롤백.
**검증**: m4a 9/9 슬라이스 통과 (elapsed 3470s, rtfx 1.44).

### 교훈
- device-side assert 는 `CUDA_LAUNCH_BLOCKING=1` 로 진짜 위치부터 확인할 것 (비동기 가짜 위치 주의).
- 단독 재현 실험(슬라이스 1만 실행)으로 "콘텐츠 의존 vs 호출간 상태" 를 가른 것이 결정적.

---

## 3. 문제 2 — 반복 hallucination

### 발견
60s pipeline transcript 의 토큰 32~36% 가 반복 구간에 갇힘: `네x256`, `보호소x85`, `Web.x152`,
`베지 저 베지...`. 무음/저정보 구간에서 greedy 디코더가 직전 토큰을 반복 선택하는 자기강화 루프.

### 크래시와의 관계
**크래시와 반복은 같은 현상의 정도 차이.** 반복 루프가 1024 토큰을 넘으면 크래시(문제 1),
안 넘으면 출력 오염(문제 2). `max_new_tokens` cap 은 크래시(증상)만 막고, 반복(원인)은 그대로.

### 해결: repetition_penalty
이미 생성된 토큰의 확률을 깎아 루프를 탈출시킴.

A/B 검증 (`tools/test_rep_penalty.py`, 2구간×3설정):

| 구간 | 설정 | 반복% | 비고 |
|---|---|---|---|
| 3100-3160s (반복97%) | baseline | 97% | `베지 저 베지...` |
| 〃 | **rp1.2** | **0%** | 실제 발화 복구 (garbage 1526자→내용 377자) |
| 〃 | rp1.3+norep3 | 0% | 이름 garbling 약간 더 |
| 20-80s (정상) | baseline | 8% | |
| 〃 | **rp1.2** | **0%** | 본문 보존 (오히려 더 완전) |

→ `no_repeat_ngram_size=3` 은 이름 garbling↑ + 정당한 짧은 반복("네 네", 숫자) 손상 위험으로 제외.
**`repetition_penalty=1.2` 단독 채택.** 적용처: 3개 generate 호출 전부
(`run_10m_slice.py:REPETITION_PENALTY`, `CohereASRBackend.REPETITION_PENALTY`).

---

## 4. 통합 결과 — 모드 × rp 매트릭스 (m4a 83분, 클로바 reference)

| 모드 | rp 없음 (WER / 반복 / RTFx) | **rp1.2 (WER / 반복 / RTFx)** |
|---|---|---|
| 60s pipeline | 1.073 / 36.0% / 1.65 | 0.652 / 3.0% / 2.23 |
| **10분 슬라이스** | 0.890 / 28.7% / 1.44 | **0.529 / 0.0% / 2.75** |

(클로바 ref 토큰=11,497 / 10분슬라이스 rp1.2 토큰=9,223 → 반복 제거로 ref 에 근접)

핵심:
- **rp1.2 는 두 모드 모두 대폭 개선** (반복 제거 + WER 하락 + 속도 향상).
- **같은 rp1.2 조건에서도 10분 슬라이스가 60s pipeline 우위** (WER 0.529 vs 0.652, 반복 0% vs 3%).
  → "청크 경계가 적을수록(9 vs 100) 좋다"는 이점은 반복 아티팩트가 아니라 **구조적**임이 공정 비교로 확증.
- 60s 는 rp1.2 후에도 잔여 반복 3% — 경계 100개에서 짧은 루프 일부 잔존. 10분 슬라이스는 0%.
- WER 0.529 가 여전히 높은 건 클로바 ref 분절/정규화 차이 + 실세계 회의 난이도. 반복 제거 전(0.890)과
  달리 이제 **측정값이 의미를 가짐**.

### repetition_penalty 1.2 vs 1.3 (값 확정)
1.3 단독 A/B(`test_rp_penalty.py`): 최악 구간은 1.2 가 이미 0% 라 1.3 도 0%(개선 없음), 정상 발화에서
1.3 이 미세하게 더 distortion(`되게 PC`→`대기 PC`, `마이스톤`→`발스톤`). → **목표 달성하는 최소값 1.2 채택**,
1.3 불필요. (repetition_penalty 1.1~1.3 은 표준 범위, 1.2 는 보수적 값)

---

## 5. 버전 관리 / 데이터 정책 / 배포

- **lb-note 독립 git 레포** (`main`). 워크스페이스 레포와 분리.
- git 커밋 3개:
  - `8496367` 초기 커밋 (10분 청킹 fix 포함)
  - `677c4ff` repetition_penalty=1.2 → **feat/vad 분기 base**
  - `7c9d0d7` rp1.2 검증 결과 문서화
- **git 제외**: `models/`(3.9G)·`samples/`(음성)·`output/`·`.env`·`.omc/`. `answer/*` 제외하되
  클로바 정답 transcript 1건만 추적.
- **데이터 이동**: 모델·음성은 git 아닌 **Google Drive 로 별도 이동**. README 에 모델 다운로드
  (`hf download CohereLabs/cohere-transcribe-03-2026`) 문서화.
- **배포 계획**: 코드만 GitHub **private** push (VAD 구현 완료 후) → 이후 Docker 이미지화(4090 이전).

### feat/vad 조율
VAD(다른 세션)는 `[10분 청킹 + repetition_penalty]` 가 main 에 커밋된 **`677c4ff` 위에서 분기**한다.
그래야 베이스 정합 + VAD 효과를 반복 제거된 깨끗한 출력 위에서 검증 가능.

---

## 6. 남은 작업

- ⏸️ **VAD 통합** (다른 세션): `audio_io.load_audio` 직후 무음 제거 → 반복 트리거 자체 감소.
  타임스탬프 재정렬 필요. feat/vad 에서 진행.
- ⏸️ **GitHub private push**: VAD 완료 후.
- ⏸️ **Dockerfile**: CUDA12.1 base. 미결정 — 모델 bake vs 볼륨 / 앱코드 bake vs 마운트.
- (선택) 60s pipeline + rp1.2 재실행 비교, 합성 wav 회귀(13/13 + WER 0.054 유지) 재확인.

---

## 부록 — 명령어 & 파일

```bash
cd /home/evan/Claude_workspace/lb-note
# 10분 슬라이스 (rp1.2 적용, 안정)
uv run python tools/run_long_slice10m.py "samples/<파일>" --out output
# 반복 A/B
uv run python tools/test_rep_penalty.py
# 슬라이스 단독 디버그
uv run python tools/test_pad_2slices.py 1
```

- 진단 도구: `tools/diag_slice_shapes.py`, `tools/test_pad_2slices.py`, `tools/test_rep_penalty.py`
- 진단 로그: `output/diagnosis/`
- 진행 노트: `SESSION_STATE.md` / 메모리 `project-lb-note-phase0`, `project-stt-chunking-cohere`
- 폐기된 오진 플랜(보존): `~/.claude/plans/10-sparkling-sparrow.md` (SUPERSEDED)
