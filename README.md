# lb-note — Cohere 한국어 회의 STT 파이프라인

`CohereLabs/cohere-transcribe-03-2026` 모델로 한국어 회의 음성을 텍스트로 변환하는 파이프라인.
다양한 입력 형식(wav/m4a/mp3/aac/amr)과 장시간(최대 180분) 음성을 지원한다.

> ⚠️ **이 레포에는 모델 가중치(3.9GB)와 샘플 음성이 포함되어 있지 않습니다.**
> git 부적합 대용량 자산이라 제외했습니다 → 아래 [모델 다운로드 & 배치](#모델-다운로드--배치) 를 먼저 수행해야 실행됩니다.

---

## 요구사항

| 항목 | 버전/사양 |
|---|---|
| Python | 3.12+ |
| 패키지 관리 | [uv](https://docs.astral.sh/uv/) |
| GPU | CUDA 12.1 호환 (torch `cu121` 휠 사용) |
| VRAM | 최소 4GB (driver fallback 시) — 권장 8GB+ |
| 시스템 의존성 | `ffmpeg` (wav 외 형식 디코딩) |
| transformers | 5.5.0 (모델이 `trust_remote_code` 커스텀 클래스 사용) |

```bash
# 시스템 의존성
sudo apt-get install -y ffmpeg
# 파이썬 의존성 — torch/torchaudio 는 GPU 별 extra 라 반드시 하나 선택:
uv sync --extra cu121    # RTX 4090 등 Ada (CUDA 12.1)
# uv sync --extra cu128  # RTX PRO 6000 등 Blackwell (CUDA 12.8+)
```

> `uv sync` (extra 없이) 는 torch 가 설치되지 않습니다 — 멀티 GPU 지원 위해 cu121/cu128 을 상호 배타 extra 로 분리했기 때문.

---

## 모델 다운로드 & 배치

모델은 git에 없으므로 직접 받아 **`models/cohere-transcribe-03-2026/`** 에 배치한다.

- HuggingFace repo: **`CohereLabs/cohere-transcribe-03-2026`**
- 라이선스: apache-2.0 (HF 로그인/`HF_TOKEN` 이 필요할 수 있음)
- 용량: 약 3.9GB (`model.safetensors`) + 커스텀 코드/토크나이저

**방법 A — hf CLI (권장)**
```bash
# huggingface_hub 1.x 부터 CLI 이름이 huggingface-cli → hf 로 변경됨
uv run hf download CohereLabs/cohere-transcribe-03-2026 \
  --local-dir models/cohere-transcribe-03-2026
```

**방법 B — Python (snapshot_download)**
```bash
uv run python -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='CohereLabs/cohere-transcribe-03-2026',
                  local_dir='models/cohere-transcribe-03-2026')
"
```

**배치 검증**
```bash
uv run python -c "from src import config; print(config.env_status())"
# cohere_model_exists: True 면 정상
```

배치 후 디렉토리에 `model.safetensors`, `config.json`, `modeling_cohere_asr.py`,
`tokenizer.json` 등이 있어야 한다.

---

## 입력 데이터 (samples)

샘플 음성도 git에 없다. 변환할 음성 파일을 `samples/` 에 두고 경로로 지정한다.
지원 형식: `.wav .m4a .mp3 .aac .amr` (그 외는 `ValueError`). 길이 한계 180분.

---

## 환경변수 (`.env`)

`.env.example` 를 복사해 `.env` 로 만든다. **`.env` 는 git/이미지에 포함하지 않는다.**

```bash
cp .env.example .env
```

| 변수 | 기본값 | 비고 |
|---|---|---|
| `COHERE_MODEL_PATH` | `<repo>/models/cohere-transcribe-03-2026` | 상대 기본값이라 이식 OK |
| `SAMPLES_DIR` | (절대경로) | ⚠️ 기본값이 특정 머신 절대경로 → **다른 환경에선 반드시 재설정** |
| `OUTPUT_DIR` | `<repo>/output` | |
| `COHERE_DTYPE` | `bfloat16` | OOM 시 `int8` 자동 폴백 |
| `HF_TOKEN` | (빈값) | 모델 다운로드 시에만 필요할 수 있음 |

---

## 사용법

```bash
# 1) 단일 호출 — 짧은 파일(~10분 이하), stdout 출력
uv run python run.py "samples/foo.wav"

# 2) 60s pipeline — 장시간 안정 경로, text.json + transcript.md 생성
uv run python run.py "samples/foo.m4a" --pipeline --out output

# 3) 10분 슬라이스 — 최고 정확도/속도, 장시간 음성
uv run python tools/run_long_slice10m.py "samples/foo.m4a" --out output

# WER 평가 (reference 있을 때)
uv run python run.py "samples/foo.m4a" --pipeline --reference "answer/ref.txt" --out output
```

### 처리 모드 비교

| 모드 | 외부 청크 | max_new_tokens | 안정성 | 비고 |
|---|---|---|---|---|
| 단일 호출 | 없음 | 256 | ~10분까지 | 그 이상 encoder OOM |
| 60s pipeline | 60s/10s | 512 | 안정 | 장시간 기본 경로 |
| 10분 슬라이스 | 600s/5s | 1000 | 안정 | 최소 청크 = 최고 정확도 |

---

## 알려진 특성 / 주의

- **디코더 position embedding 한계 = 1024** (`config.transf_decoder.max_sequence_length`).
  내부 30초 청크별 생성 토큰이 1024를 넘으면 `pos_emb` 인덱스 초과로 CUDA assert 발생.
  → `max_new_tokens` 는 반드시 **1024 미만**으로 둔다 (10분 슬라이스는 1000).
- **반복 hallucination**: 무음/저정보 구간에서 greedy 디코더가 한 토큰을 반복 생성.
  현재 절대 WER을 부풀리는 주요 원인 → `repetition_penalty`/`no_repeat_ngram_size` 도입 예정.
- 입력은 16kHz mono float32 로 정규화하며, `peak > 0.5` 면 클리핑 제거 스케일링을 적용한다.

진행 상황과 상세 진단은 `SESSION_STATE.md` 참조.

---

## 디렉토리 구조

```
lb-note/
├── run.py                      # 진입점 (단일/파이프라인 모드 분기)
├── src/
│   ├── audio_io.py             # 디코딩·16kHz mono 정규화
│   ├── chunker.py              # 청크 분할 + overlap dedupe
│   ├── pipeline.py             # 60s pipeline 통합 흐름
│   ├── scoring.py              # WER/CER + reference 포맷 감지
│   ├── stt.py / config.py      # 백엔드 팩토리 / 설정
│   └── backends/cohere.py      # Cohere 백엔드 (단일/배열 transcribe)
├── tools/
│   ├── run_long_slice10m.py    # 10분 슬라이스 장시간 변환
│   ├── run_10m_slice.py        # 슬라이스 코어 (transcribe_slice 공유)
│   └── diag_slice_shapes.py, test_pad_2slices.py  # 진단 도구
├── models/   (git 제외)        # ← 모델 가중치 배치 위치 (Google Drive 등으로 별도 이동)
├── samples/  (git 제외)        # ← 입력 음성 배치 위치
├── answer/                     # 평가용 reference transcript (ax_tf_클로바.txt 만 추적)
└── output/   (git 제외)        # 결과물
```

> 🔒 **민감 데이터 정책**: 대용량 모델/합성 음성과 실제 회의 **음성 파일**은 git/원격에 올리지 않고
> Google Drive 등 별도 경로로 이동한다. (회의 정답 transcript `answer/ax_tf_클로바.txt` 는
> 평가 reference 로 소유자 판단에 따라 레포에 포함.) private 레포라도 외부 서버 업로드 +
> 히스토리 영구 잔존 위험이 있으므로 음성·개인정보 원본은 제외 원칙을 유지한다.
