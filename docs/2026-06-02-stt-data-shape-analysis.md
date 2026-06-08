# STT 산출 JSON 데이터 형태 분석 — 가장 적절한 소비 파이프라인 도출

- 작성일: **2026-06-02**
- 대상: `output/*.json` 6개 (STT run 5 + audio_quality 1)
- 방법: **사전 규약·스킬을 강요하지 않고**, JSON 자체를 귀납적으로 분석(필드 카탈로그 추출)해
  데이터가 자연스럽게 원하는 소비 형태를 도출.
- 도구: `tools/normalize_runs.py` (정규화), introspection 결과(아래 §1)

---

## 0. 결론 요약

생성된 JSON은 **공통 코어는 있으나 스키마가 드리프트**(같은 개념이 여러 이름)되어 있어
"그대로 쓰기"엔 부적절하다. 데이터를 분석한 결과, 가장 적절한 형태는 **4개 뷰로 분해**하는 것이다:

1. **`runs.json`** — run 당 정규 레코드(식별·설정·지표·성능·참조·페이로드요약)
2. **`comparison.csv/md`** — run×핵심지표 1행 (분석·비교용 denormalized 뷰)
3. **`metrics_long.csv`** — `(run_id, metric, value)` tidy/long (플로팅·집계)
4. **`audio_quality.json`** — audio 당 음질 레코드 (`audio_id`로 run과 조인)

이 형태는 기존 5종 비교표를 **무손실 재구성**함이 검증됐다(§4). `tools/normalize_runs.py`가 생성한다.

---

## 1. 필드 카탈로그 (귀납 추출)

6개 JSON에서 **고유 필드경로 109개**를 추출. 등장빈도 상위 = 안정적 코어:

- **6/6**: `generated_at`
- **5/6 (STT 코어)**: `mode`, `schema_version`, `transcript`, `audio.source_path`,
  `audio.duration_seconds`, `model.{backend,name,repetition_penalty}`,
  `performance.{elapsed_seconds,rtfx,vram_peak_mb}`,
  `evaluation.{wer,cer,repetition_ratio,repetition_burst_count,hyp_tokens,ref_tokens,hyp_ref_token_ratio,ref_source,reference_path}`

→ 즉 **식별·지표·성능의 코어는 일관**되지만, 그 바깥(설정·전처리·분할·baseline)이 흔들린다.

## 2. 드리프트 리포트 (데이터가 드러낸 불일치)

| 개념 | 관측된 키들 (같은 의미, 다른 이름/모양) |
|---|---|
| 적용 향상 | `preprocess.applied[]` · `preprocess.enhancers[]` · `preprocess.enhancers_applied[]` (**3종**) |
| baseline 비교 | `baseline_single_call{}` · `baseline_no_preprocess{}` (**2종**, 내용 동일) |
| 분할 설정 | `pipeline{slice_sec,overlap_sec,n_slices}` · `pipeline{mode,sliced,audio_chunk_index}` · `chunking{method,target_sec,...}` (**한 키가 3가지 모양**) |
| 길이 | `audio.duration_seconds` · `duration_sec` (audio_quality) |
| `audio` 타입 충돌 | 객체(`audio.source_path`) vs 문자열(`audio`) |
| 중복 | `vad_regions` 가 `snr{}` 와 `chunking{}` 양쪽에 동일값 |
| 성능 타이밍 | `decode_seconds`/`generate_seconds`, `preprocess_seconds`/`enhance_seconds` 혼재 |
| 누락 | `decode_tuning` JSON 부재(beam 크래시로 미저장) |

### STT JSON 섹션 존재 매트릭스

| file | preprocess | pipeline | chunking | segments | baseline_no | baseline_single |
|---|---|---|---|---|---|---|
| single_call | O | O | - | - | - | - |
| single_call_enhanced | O | O | - | - | **O** | - |
| slice10m | - | O | - | - | - | **O** |
| vad_chunk | - | - | O | O | - | O |
| vad_chunk_enh | O | - | O | O | - | O |

→ 선택적 섹션 + 이름 불일치 + 타입 충돌. **단일 스키마로 가정하면 코드가 깨진다.**

## 3. 도출된 정규 형태 (왜 4뷰인가)

소비 시나리오별로 데이터가 원하는 모양이 다르다 → 관심사 분리:

| 뷰 | 용도 | 한 행/레코드의 단위 |
|---|---|---|
| `runs.json` | run 상세·재현 | run (식별+설정+지표+성능+참조) |
| `comparison.csv/md` | 모드 간 비교 | run × 핵심지표 (denormalized) |
| `metrics_long.csv` | 집계·플로팅 | (run_id, metric, value) tidy |
| `audio_quality.json` | 음원 특성 | audio (`audio_id`로 run과 조인) |

**정규화 규칙(드리프트 흡수, fallback 체인):**
- `run_id` = 파일명에서 도출 (`mode` 충돌 회피: vad_chunk vs vad_chunk_enh).
- enhancers = `applied ‖ enhancers ‖ enhancers_applied` 중 첫 비어있지 않은 값.
- baseline = `baseline_single_call ‖ baseline_no_preprocess`.
- segmentation = `chunking{} ‖ pipeline{slice} ‖ pipeline{single}` → `{strategy,n_units,...}` 단일 블록.
- duration = `audio.duration_seconds ‖ duration_sec`; `audio_id` = 경로의 basename.
- 무거운 페이로드(transcript/segments)는 레코드에서 **분리**(길이·개수만 요약, 원본 JSON 참조).

## 4. 검증 — 무손실 재구성

`normalize_runs.py` 출력이 기존 score-*.md 수치와 **완전 일치**(정규 형태만으로 비교표 복원 가능):

| run_id | enhancers | segmentation | WER | CER | RTFx | VRAM(MB) | segments |
|---|---|---|---|---|---|---|---|
| single_call | none | single_call | 0.4199 | 0.3017 | 332.07 | 20828 | 0 |
| single_call_enhanced | wpe+gtcrn+silero | single_call | 0.4244 | 0.2999 | 10.34 | 20390 | 0 |
| slice10m | none | slice | 0.4247 | 0.3101 | 384.5 | 6033 | 0 |
| vad_chunk | none | silero_vad_segmentation | 0.4169 | 0.3038 | 51.57 | 4043 | 213 |
| vad_chunk_enh | wpe+gtcrn | silero_vad_segmentation | 0.4269 | 0.3065 | 9.74 | 4043 | 208 |

audio_quality: SNR 17.27dB / cutoff 5922Hz / clip 0.0% / dyn 11.95dB / speech 90.3% (1 레코드)

→ "이 4뷰면 충분하다"가 증명됨. 드리프트 3종(enhancers)·2종(baseline)·3모양(segmentation)이 모두 단일 컬럼으로 수렴.

## 5. 권고 — 데이터 사용 파이프라인

```
output/*.json  ──(normalize_runs.py)──▶  output/normalized/
   text-*.json  ─┐                          ├─ runs.json          (정규 run 레코드)
   audio_quality ┘                          ├─ comparison.csv/md  (비교 뷰)
                                            ├─ metrics_long.csv   (tidy, 플로팅)
                                            └─ audio_quality.json (음원 뷰)
```

- **생산 측 권고(후속 실험 스크립트)**: 새 run을 쓸 때도 드리프트 키를 만들지 말고, 향후에는
  `normalize_runs.py`의 정규 스키마를 **그대로 출력**하면 변환 단계가 사라진다(특히 enhancers/baseline/segmentation 키 통일).
- **소비 측 권고**: 분석/대시보드는 `comparison.csv`·`metrics_long.csv`만 보면 됨. 원본 JSON은 재현·감사용.
- **누락 보강**: `decode_tuning` run은 JSON 미저장 → 재실행 시 정규 스키마로 남기면 비교에 자동 편입.

## 6. 산출물 · 도구

- `tools/normalize_runs.py` — 정규화 변환기(4뷰 생성 + stdout 검증표)
- `output/normalized/runs.json`, `comparison.csv`, `comparison.md`, `metrics_long.csv`, `audio_quality.json`
- 본 문서: 필드 카탈로그·드리프트·정규 형태·검증·권고
