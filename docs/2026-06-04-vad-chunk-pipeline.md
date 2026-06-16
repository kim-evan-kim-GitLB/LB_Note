# vad_chunk(energy) 파이프라인 메인 승격 (2026-06-04)

## 요약

검증된 STT 청킹 방식 **vad_chunk(energy)** 를 일회성 실험 스크립트
(`tools/vad_chunk_ax_clova.py`)에서 메인 파이프라인(`src/pipeline.py` ← `run.py`)으로
승격하고 **기본값**으로 채택했다. 기존 고정 길이 청킹과 단일 호출(single_call) 모드는
하위호환으로 유지한다.

## vad_chunk(energy) 란

모델 내부 에너지 청커는 우리 VAD 경계를 모르고 자체 ~32~35s 그리드로 입력을 재분할한다.
그래서 단어 중간이 잘리고 경계 품질이 나빠진다. 이를 해결하는 "되는 버전"이 vad_chunk:

### 3가지 핵심 요소

1. **VAD 기반 분할(segmentation)** — `src/chunker.py: vad_segment_chunks()`
   VAD 로 발화 구간을 찾고, 인접 발화를 ≤`target_sec`(기본 30s, < 모델 `max_audio_clip_s`=35)
   로 greedy 하게 묶는다. 컷은 항상 발화 사이 **무음 gap** 에 떨어져 단어 절단이 사라진다.
   각 청크가 35s 미만이라 모델 내부 청커가 재분할하지 않는다. 단일 발화가 target 을
   초과하면 `overlap_sec`(기본 2.0s)을 주고 hard-split 하며, 그 seam 만 dedup 대상으로 표시한다.

2. **배치 디코딩** — `src/backends/cohere.py: transcribe_arrays()`
   청크들을 배치(기본 `batch_size`=8)로 디코딩(processor batch path → `generate()` →
   `batch_decode()`). greedy 라 배치 여부와 결과가 동일하며 ~3.8x 빠르다.
   `batch_size<=1` 이면 단일 경로로 폴백한다.

3. **seam 한정 중복제거 병합** — `src/chunker.py: merge_vad_segments()`
   overlap seam 청크만 앞 청크 꼬리와 단어 중복제거(최대 `SEAM_DEDUP_MAX_WORDS`=12 단어).
   무음 경계에서 잘린 일반 청크는 중복이 없으므로 그대로 이어붙인다.

### VAD 백엔드: energy (기본)

분할용 VAD 는 `energy`(에너지 기반, 모델 없음)와 `silero` 중 선택. **energy 가 ~15x 빠르며
청킹 품질은 동등**하므로 기본값으로 채택. `src.stt.get_vad("energy")` 로 획득.
VAD `.detect(wav, sr)` 는 `[(start_sec, end_sec), ...]` 발화 구간을 반환한다.

> 주의: 기존 `--vad` 플래그(Silero **무음압축** 전처리)와 본 VAD **분할**은 별개다.
> `--vad` 는 건드리지 않으며 분할과 직교적으로 함께 동작할 수 있다.

## 왜 기본값인가 (지표)

ax 과제회의 음성(Clova Note reference) 기준 7개 run 비교
(`output/normalized/comparison.md`, `output/score-ax_vad_chunk_energy.md`):

| run | WER ↓ | CER ↓ | RTFx ↑ | VRAM(MB) | segments | 타임스탬프 |
|---|---|---|---|---|---|---|
| single_call (baseline) | 0.4199 | 0.3017 | 332.07 | 20828 | 0 | 불가 |
| slice10m (고정) | 0.4247 | 0.3101 | 384.5 | 6033 | 0 | 불가 |
| **vad_chunk_energy** | **0.4173** | **0.2987** | **232.59** | **4707** | **215** | **가능** |
| vad_chunk (silero) | 0.4169 | 0.3038 | 51.57 | 4043 | 213 | 가능 |
| vad_chunk_enh_energy | 0.4273 | 0.3089 | 10.72 | 4707 | 218 | 가능 |

- vad_chunk_energy: **WER 0.417 / CER 0.299** (baseline 대비 WER·CER 모두 개선),
  **VRAM 4GB**(single_call 20GB 대비 1/4 이하), **RTFx 232**, 그리고 **타임스탬프** 제공.
- energy 는 silero 대비 RTFx 가 압도적(232 vs 51)이고 CER 은 오히려 더 낮다.
- 음향 향상(wpe+gtcrn) 변형은 WER/CER 모두 **악화**된다(net-negative).
  배경: `/app/docs/2026-06-04-stt-accuracy-judgment.md` 참조 — 이 음원은 노이즈가 아니라
  6kHz 대역제한이 한계라 향상이 도움이 안 된다.

## CLI 사용법

기본(파이프라인 = vad_chunk(energy)):

```bash
# reference/out 지정 시 자동 파이프라인. 기본 청킹이 vad_chunk(energy).
sudo .venv/bin/python run.py samples/audio.m4a --reference answer/ref.txt --out output

# 명시적 파이프라인
sudo .venv/bin/python run.py samples/audio.m4a --pipeline --out output
```

새 플래그(기본값):

```bash
--segmentation {vad,fixed}    # 기본 vad. fixed = 기존 고정 길이 청킹(하위호환)
--vad-backend {energy,silero} # 기본 energy(~15x 빠름)
--batch-size INT              # 기본 8 (greedy 라 결과 동일, 가속용)
--target-sec FLOAT            # 기본 30.0 (<35s 라야 내부 청커 재분할 없음)
```

기존 고정 길이 청킹 재현(하위호환):

```bash
sudo .venv/bin/python run.py samples/audio.m4a --pipeline --segmentation fixed \
    --chunk-sec 60 --overlap-sec 10 --out output
```

단일 호출 모드(슬라이스 없음)는 플래그 없이 그대로 사용한다(`tools/single_call_ax_clova.py` 참조).

## 메인 승격 검증 및 발견된 수정 (중요)

승격 후 `run.py --segmentation vad --vad-backend energy` 로 동일 음원을 돌려 실험본
(`tools/vad_chunk_ax_clova.py` 산출 `output/text-ax_vad_chunk_energy.json`)과 대조했다.
처음엔 청크 경계(VAD regions 713 → 215청크)가 완전히 동일한데도 transcript 가 달랐다
(WER 0.417→0.490). 원인을 추적해 **메인 경로에만 있던 잠복 버그 2개**를 고쳤다.

1. **`src/audio_io.py` — 진폭 0.5배 감쇠 제거.**
   기존 `load_audio()` 는 `peak>0.5` 면 음원을 0.5 로 감쇠시켰다. 그런데 Cohere 의 mel
   프론트엔드는 입력 진폭 스케일에 민감해, 0.5배 음원에서 STT 품질이 저하된다. 실험
   baseline 들은 전부 `librosa.load`(peak 1.0)를 직접 썼으므로 이 감쇠가 없었고, 그래서
   메인 파이프라인만 조용히 나빠지고 있었다. → **클리핑 가드(`peak>1.0` → 1.0)로 교체**.
   확인: 두 경로 파형은 정확히 0.5배 스케일 차이뿐(스케일 제거 후 잔차 0.0)이었다.

2. **`src/scoring.py` — Clova `.txt` 화자 헤더 제거.**
   `load_reference_text()` 가 `.txt` 를 무조건 `plain_txt` 로 읽어 화자 헤더
   (`참석자 N MM:SS`)를 reference 토큰에 남겼다(CLAUDE.md 의 헤더 제거 규칙 위반). 게다가
   실험 도구의 옛 regex 는 **1시간 이후 헤더(`참석자 N 1:02:33`)를 놓쳤다.** → 두 경우 모두
   제거하도록 `CLOVA_SPEAKER_HEADER_RE`(시·분·초 선택) 추가, 헤더가 검출되면 `clova_note_txt`
   로 라벨링.

### 동치 입증

| 비교 | WER | CER |
|---|---|---|
| 메인(수정후) — 실험본과 **동일 reference** | **0.4173** | **0.2987** |
| 실험본 vad_chunk_energy | 0.4173 | 0.2987 |
| 메인 — 헤더 완전제거 reference(개선된 scoring) | 0.3989 | 0.2574 |

- 수정 후 메인 파이프라인 transcript 는 실험본과 **바이트 단위로 동일**(37702자). 같은
  reference 로 채점하면 **0.4173 / 0.2987 정확히 일치** → 승격이 동치임을 입증.
- 부수 성과: 시간대 헤더까지 제거한 깨끗한 reference 로 보면 실제 품질은 **WER 0.399 /
  CER 0.257** 로, 이전 0.417/0.299 는 reference 에 남아 있던 헤더 토큰(~456개)이 부풀린 값이었다.
- 주의: 수정 1은 **모든 파이프라인 경로**(고정 청킹 포함)의 입력 진폭을 바꾼다 — STT 에는
  개선이지만 고정 모드는 별도 WER 재검증을 권한다. 수정 2는 향후 Clova `.txt` 채점값을
  ~0.02 낮추므로, 과거 `comparison.md` 의 baked-in 수치(옛 reference 기준)와 직접 비교 시 유의.

## 한계 / 후속

- 현재 검증은 **단일 음원**(ax 과제회의, Clova Note reference) 한 건에 한정된다.
  reference 자체가 Clova STT 결과(ground truth 아님)이므로 WER 절대값보다 패턴·Δ 신호로 해석한다.
- **권장: 추가로 1~2개 음원에서 vad_chunk(energy) 를 검증**해 일반화 여부를 확인할 것.
