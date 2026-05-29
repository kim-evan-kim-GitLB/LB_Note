# SESSION STATE — lb-note STT 작업 이어가기

> 세션을 정리하고 돌아와도 이 문서만 읽으면 작업을 그대로 이어갈 수 있도록 정리한 진행 노트.
> 최종 갱신: 2026-05-29

---

## TL;DR (지금 어디까지 왔나)

- **목표**: 다양한 음성 파일을 Cohere transcribe 모델로 텍스트 변환하는 파이프라인.
- **현재 상태**: m4a 회의 음성(83.2분) transcript 확보 — 60s pipeline + **10분 슬라이스 둘 다 동작**.
- **✅ 해결됨 (2026-05-29)**: 10분 슬라이스 2번째 슬라이스 CUDA assert. **진짜 원인은 "mask invariant"가 아니라 디코더 position embedding(1024행) 인덱스 초과** — 반복 루프 청크가 max_new_tokens=4096 동안 1024 토큰을 넘겨서 발생. fix = `MAX_NEW_TOKENS 4096→1000`. m4a 9/9 통과 검증.
- **다음 할 일**: 반복 hallucination 제거(`repetition_penalty`/`no_repeat_ngram_size`). 두 모드 모두 반복 28~36% 잔존 → 절대 WER 무의미. + (선택) 합성 wav 회귀 13/13 재확인.

---

## 환경

- 메인 프로젝트: `/home/evan/Claude_workspace/lb-note/` (전엔 `meeting-poc-cohere`, 2026-05-27 rename)
- archive: `/home/evan/Claude_workspace/lb-note-archive/` (Qwen 비교 산출물)
- 모델: `lb-note/models/cohere-transcribe-03-2026/` (bf16, transformers 5.5.0)
- GPU: RTX 3050 Ti Laptop, **VRAM 4GB** (driver fallback 사용)
- venv: `uv` 관리. 실행은 `cd lb-note && uv run python ...`

---

## 입력 정책 (2026-05-27 확정, `src/audio_io.py`)

- 지원 형식: `.wav .m4a .mp3 .aac .amr` (그 외 ValueError). **mp4 등 비디오 컨테이너 거부**.
- 길이 한계: **180분** (`MAX_DURATION_SEC = 10800`).
- 내부 정규화: `peak > 0.5` 면 `samples *= 0.5/peak` (클리핑 제거). **silence prepend 는 결과 망가뜨려 제거됨**.
- 모든 입력은 16kHz mono float32 로 통일.

---

## 처리 모드 3가지 (정확도·속도·안정성)

| 모드 | 코드 | 청크 | WER (합성 실측) | RTFx | 4GB GPU 안정성 |
|---|---|---|---|---|---|
| 단일 호출 | `run.py audio.wav` (옵션 없음) | 1 | 0.028 | 2.90 | ~10분 입력까지만 (그 이상 OOM) |
| 60s pipeline | `run.py audio --pipeline --out output` | 60s/10s | 0.101 | 1.65~2.18 | **안정 (실측 0 에러)** |
| 10분 슬라이스 | `tools/run_long_slice10m.py audio --out output` | 600s/5s | 합성 **0.054** | **3.38**/1.44 | **✅ 안정 (2026-05-29 fix 후 9/9)** |

→ 10분 슬라이스가 가장 빠르고 정확. **2026-05-29 CUDA assert 해결**(max_new_tokens cap). 실세계 m4a 품질 비교: 10분슬라이스(WER 0.890/반복 28.7%) > 60s pipeline(1.073/36.0%). 단 두 WER 모두 반복 garbage 로 부풀려진 값.

---

## 이번 세션 산출물 (m4a 회의 transcript)

**채택 결과 (60s pipeline)**:
```
output/transcript-ax과제회의(클로바노트)_음성파일.md   ← 본문 59,509자
output/text-ax과제회의(클로바노트)_음성파일.json       ← 메타 + 100 segments + transcript
```
- 입력: `samples/ax과제회의(클로바노트)_음성파일.m4a` (4992.51s = 83.2분, AAC 16kHz mono)
- 처리: 50.35분(3020.75s), RTFx 1.65, VRAM peak 4168MB, 100/100 청크, CUDA assert 0건
- 한국어 회의 본문 정상 추출 확인

**10분 슬라이스 최종 결과 (2026-05-29, repetition_penalty=1.2 적용본)**:
```
output/transcript-ax과제회의(클로바노트)_음성파일_slice10m.md   ← 본문 35,064자
output/text-ax과제회의(클로바노트)_음성파일_slice10m.json
```
- 9/9 슬라이스 통과(크래시 0), elapsed **1818s**, **RTFx 2.75**, VRAM peak 6032MB
- 클로바 reference 대비 **WER 0.529 / 반복 0.0%**

| 모드 | WER | 토큰 | 반복% | RTFx |
|---|---|---|---|---|
| 60s pipeline (rp없음) | 1.073 | 16352 | 36.0% | 1.65 |
| 10분슬라이스 (rp없음) | 0.890 | 14558 | 28.7% | 1.44 |
| **10분슬라이스 rp1.2** | **0.529** | **9223** | **0.0%** | **2.75** |

→ rp1.2 로 반복 완전 제거 + WER 40%↓ + 속도 2배. 회귀 없음 → **feat/vad base(`677c4ff`) 확정**.

**참고**: `answer/ax_tf_클로바.txt` 가 클로바노트 정답 transcript → WER 평가 reference (git 추적 포함).

---

## 핵심 진단 — 10분 슬라이스 CUDA assert (✅ 2026-05-29 해결)

**이전 진단(2026-05-28)은 오진**: "energy-based chunking → encoder seq_len 변동 → SDPA mask invariant 위반"은 틀렸음. `masking_utils.py:326 padding_mask.all()` 은 async CUDA 에러가 GPU→host 동기화 지점에서 surface 된 **가짜 위치**였음.

**진짜 원인** (`CUDA_LAUNCH_BLOCKING=1` 로 확보): `modeling_cohere_asr.py:387 pos_emb = self.pos_emb(position_ids)` 의 **embedding index out-of-range**.
- 디코더 position embedding 테이블 = `config.transf_decoder.max_sequence_length = 1024` 행
- generate 는 internal 30초 청크(batch=19) 단위 독립 생성. 정상 발화는 수백 토큰
- **반복 hallucination 루프**에 빠진 청크가 토큰을 계속 생성 → position_ids 가 1024 초과 → assert
- 60s pipeline(max_new=512<1024): 크래시 없이 반복 garbage 만 / 10분슬라이스(max_new=4096>1024): 1024 넘겨 크래시 / 합성 wav: 루프 자체가 없어 통과

**해결**: `tools/run_10m_slice.py` `MAX_NEW_TOKENS = 4096 → 1000`. (오진 기반 encoder 패딩은 도입했다 제거. model reload per slice 도 롤백 완료.)
**진단 도구** (보존): `tools/diag_slice_shapes.py`(shape 측정), `tools/test_pad_2slices.py`(슬라이스 인덱스 지정 generate). 로그: `output/diagnosis/`.

---

## 반복 hallucination 제거 (✅ 2026-05-29 적용)

A/B 검증(`tools/test_rep_penalty.py`, 2구간×3설정):
- **3100-3160s (반복 97%)** → rp1.2 적용 시 **0%**, 묻혀있던 실제 발화 복구 (garbage 1526자 → 내용 377자)
- **20-80s (정상 발화)** → 보존됨(오히려 더 완전). 오인식은 baseline에도 있던 모델 고유 현상.
- `no_repeat_ngram_size=3` 은 이름 garbling↑ + 정당한 짧은 반복 손상 위험 → 제외, **`repetition_penalty=1.2` 단독 채택**.
- 적용: `tools/run_10m_slice.py:REPETITION_PENALTY`, `src/backends/cohere.py:CohereASRBackend.REPETITION_PENALTY` (3개 generate 호출 전부).
- 이 커밋이 **feat/vad 분기 base** (다른 세션이 이 위에서 VAD 분기·검증).

## 다음 할 일 (우선순위)

0. **[✅ 완료] rp1.2 전체 재실행 검증**: 9/9 통과, WER 0.890→0.529, 반복 28.7→0%, RTFx 1.44→2.75. 회귀 없음 → feat/vad base 확정.
1. **feat/vad 분기** (다른 세션): main `677c4ff`(=10분청킹+rp1.2) 위에서 분기 가능. **이 신호 전달됨.**
2. **(선택) 합성 wav 회귀**: `long_synth_120m.wav` 재실행 → 13/13 + WER 0.054 유지 확인 (max_new 1000 이 합성 정상 청크를 truncate 안 하는지 — 이론상 안전하나 미검증).
3. **VAD 통합** (다른 세션 논의 중): 무음 제거로 반복 트리거 자체 감소. `audio_io.load_audio` 직후 ~ `chunk_audio` 전에 삽입. 타임스탬프 재정렬 필요.

**참고 명령어**:
```bash
cd /home/evan/Claude_workspace/lb-note
uv run python tools/run_long_slice10m.py "samples/<파일>" --out output   # 10분 슬라이스 (이제 안정)
uv run python tools/test_pad_2slices.py 1                                # 특정 슬라이스만 디버그
```

---

## 명령어 cheat sheet

```bash
cd /home/evan/Claude_workspace/lb-note

# 60s pipeline (현재 안전 경로)
uv run python run.py "samples/<파일>" --pipeline --out output

# 10분 슬라이스 (2026-05-29 fix 후 안정, 최고 정확도)
uv run python tools/run_long_slice10m.py "samples/<파일>" --out output

# 스모크: audio_io 로딩만 검증
uv run python -c "from src.audio_io import load_audio; from pathlib import Path; s,sr=load_audio(Path('samples/<파일>')); print(s.shape, sr)"

# WER 평가 (reference 있을 때)
uv run python run.py "samples/<파일>" --pipeline --reference "answer/ax_tf_클로바.txt" --out output
```

---

## 버전 관리 / 배포 (2026-05-29 신규)

- **독립 git 레포** 로 init 완료 (`main` 브랜치). 워크스페이스 레포와 분리.
- 추적: 코드·문서·설정 39개 파일(~660K) + `answer/ax_tf_클로바.txt`(평가 reference, 소유자 판단 포함).
- **git 제외**: `models/`(3.9G)·`samples/`(음성, wav/m4a)·`output/`·`.env`·`.omc/`. `answer/*` 는 제외하되 클로바 정답 1건만 `!` 로 허용.
- **데이터 이동 정책**: 모델·음성은 git 아닌 **Google Drive 로 별도 이동**. README 에 모델 다운로드(`CohereLabs/cohere-transcribe-03-2026`, `hf download`) 문서화.
- **배포 계획**: GitHub **private** push 예정(코드만). 이후 Docker 이미지화(CUDA12.1 base, 4090 이전) — Dockerfile 미작성.
- 미결정: 모델 bake vs 볼륨 / 앱코드 bake vs 마운트.

---

## 관련 문서

- 사용법·모델 다운로드: `README.md` (이번 세션 신규)
- 구현 플랜(⚠️ SUPERSEDED, 오진 보존용): `/home/evan/.claude/plans/10-sparkling-sparrow.md`
- 모듈화 플랜: `docs/modularization-plan.md`
- 청크 전략 실측: 메모리 `project-stt-chunking-cohere`
- 프로젝트 개요: 메모리 `project-lb-note-phase0`
