# feat/vad 핸드오프 — STT 프론트엔드 전처리 (VAD + WPE dereverb + GTCRN denoise)

> 다른 세션이 이 브랜치를 이어받아 작업할 수 있도록 정리한 인수인계 문서.
> 최종 갱신: 2026-05-31

## TL;DR

- **목표**: lb-note Cohere STT 에 프론트엔드 전처리(WPE dereverb + GTCRN denoise + Silero VAD 무음압축)를 **opt-in(기본 OFF)** 으로 추가.
- **상태**: **코드 구현·단위·통합 스모크·샘플 슬라이스 A/B 완료.** 풀스택 83분 전체 실행만 **로컬 세션 팅김으로 미완** → Docker 환경에서 완주 예정.
- **샘플 슬라이스 A/B 결과**: 풀스택(+WPE+GTCRN+VAD)이 rp1.2 baseline 대비 **WER −0.125 / CER −0.125** (개선 확인). 단 **GTCRN 단독은 +0.035 악화** → WPE 와 반드시 결합.
- **다음 작업자**: 아래 "남은 작업" 부터. 핵심은 Docker 로 83분 전체 완주 + WER 확정.

---

## 1. 브랜치 / worktree

- 브랜치: `feat/vad`, base = **`677c4ff`** (`feat: repetition_penalty=1.2`). 이 base 는 10분 청킹 + rp1.2 가 들어간 커밋(다른 세션 완료분).
- worktree 위치: `/home/evan/Claude_workspace/lb-note-vad` (메인 체크아웃 `/home/evan/Claude_workspace/lb-note` 와 별도 디렉토리).
- 메인 repo 와 **파일 충돌 없음**(disjoint). 머지 시 깔끔.

### worktree 재현 방법 (다른 환경/세션)
```bash
cd /home/evan/Claude_workspace/lb-note
git worktree add ../lb-note-vad feat/vad        # 이미 브랜치 존재 시
cd ../lb-note-vad
# gitignore 자산 링크 (.venv·models·samples·.env 전부 gitignore)
mkdir -p models output
ln -sfn /home/evan/Claude_workspace/lb-note/models/cohere-transcribe-03-2026 models/cohere-transcribe-03-2026
ln -sfn /home/evan/Claude_workspace/lb-note/.env .env
ln -sfn /home/evan/Claude_workspace/lb-note/samples samples
uv sync                                          # worktree 전용 .venv
# GTCRN 체크포인트(gitignore, ~566KB)는 fetch 필요:
#   git clone --depth 1 https://github.com/Xiaobin-Rong/gtcrn /tmp/gtcrn_src
#   cp /tmp/gtcrn_src/checkpoints/model_trained_on_dns3.tar models/gtcrn/
```

---

## 2. 아키텍처

`load_audio` → **preprocess(공유 모듈)** → 청킹 → ASR. preprocess 는 chunk-size 무관이라 **60s pipeline 경로와 10분 슬라이스 경로 양쪽**이 호출.

```
음성 → 16k 변환 → [enhancers: wpe→gtcrn] → [VAD: silero 무음압축(offset_map)]
     → 청킹 → Cohere ASR(rp1.2) → 타임스탬프 원본 remap → text.json/transcript.md
```

- **enhancers**: 전체 16k 신호에 순서대로 적용(길이 보존). WPE(dereverb) → GTCRN(denoise).
- **VAD**: Silero 로 발화 구간 검출 → gap>`VAD_MAX_SILENCE_SEC` 인 긴 무음 제거(압축) + `offset_map` 생성. **비파괴**: 출력 segment 타임스탬프를 원본 타임라인으로 remap → 향후 pyannote diarization 정렬 호환. `speech_regions`(원본 타임라인)도 메타에 보존.
- **전부 OFF 면 no-op** → 기존 출력 보존(backward-compat).

### 신규 파일
| 파일 | 역할 |
|---|---|
| `src/preprocess.py` | 공유 전처리: `preprocess()`, `PreprocessResult`, `remap_time()`, `_compress_silence()` |
| `src/backends/enhancer_base.py` | `AudioEnhancer` ABC (load/unload/process) |
| `src/backends/vad_base.py` | `VADBackend` ABC (load/unload/detect→regions) |
| `src/backends/wpe_dereverb.py` | `WPEDereverb` (nara_wpe, CPU, `tools/enhance_full.py` 로직 이식) |
| `src/backends/gtcrn_denoiser.py` | `GTCRNDenoiser` (vendored 모델, CPU, 16k) |
| `src/backends/silero_vad.py` | `SileroVAD` (silero-vad 패키지 래핑) |
| `src/backends/_vendor/gtcrn/` | vendored GTCRN 모델코드+LICENSE+NOTICE (MIT, 커밋 3862c44) |
| `tools/score_frontend.py` | 임의 transcript json 채점(WER/CER/rep_ratio) |
| `docs/denoiser-zipenhancer-plan.md` | ZipEnhancer + GTCRN ONNX 향후 계획(문서만) |

### 수정 파일 (전부 add-only)
| 파일 | 변경 |
|---|---|
| `src/stt.py` | `get_enhancer()`, `get_vad()` 팩토리 추가 |
| `src/config.py` | `ENHANCERS`, `VAD_*`, `GTCRN_MODEL_PATH`, `parse_enhancers()` 추가 |
| `src/pipeline.py` | preprocess 삽입 + 타임스탬프 remap + payload `preprocess` 블록 + `SCHEMA_VERSION 1.0→1.1` |
| `run.py` | `--dereverb/--denoise/--vad` 플래그(있으면 자동 파이프라인) |
| `tools/run_long_slice10m.py` | preprocess 삽입(slice 전) + remap + 플래그 |
| `pyproject.toml`,`uv.lock` | silero-vad, torchaudio(cu121), einops 추가 |

---

## 3. 사용법

```bash
cd /home/evan/Claude_workspace/lb-note-vad

# 60s pipeline 경로 (안정, WER/CER 채점 내장 가능)
uv run python run.py "samples/<파일>" --pipeline --dereverb --denoise --vad --out output/<dir>
#   --reference 주면 자동 WER/CER 채점

# 10분 슬라이스 경로 (정확도-속도 최적, 채점 없음 → score_frontend.py 별도)
uv run python tools/run_long_slice10m.py "samples/<파일>" --dereverb --denoise --vad --out output/<dir>

# 채점 (클로바 reference)
uv run python tools/score_frontend.py output/<dir>/text-*.json --label "<라벨>" --baseline 0.529 --out output/score-frontend-compare.md
```

플래그 없으면 전처리 미적용(기존 동작). 개별 토글 가능: `--dereverb`(WPE), `--denoise`(GTCRN), `--vad`(Silero).

### Config (env, 기본 OFF)
`ENHANCERS=""`(예 `"wpe,gtcrn"`) / `VAD_BACKEND=""`(예 `"silero"`) / `VAD_THRESHOLD=0.5` / `VAD_PAD_SEC=0.25` / `VAD_MAX_SILENCE_SEC=0.5` / `GTCRN_MODEL_PATH=models/gtcrn/model_trained_on_dns3.tar`. CLI 플래그가 env 보다 우선.

---

## 4. 검증 완료 내역

- **단위**(ax m4a 앞 60초, CPU): GTCRN(len Δ0, 1.95s)·WPE(peak 보존, 11s)·Silero(5구간, 0.6s) 동작. 무음압축+remap 정확성(인공 10s→4.5s, 압축 2.26s→원본 7.76s 점프). no-op 입력 불변·offset 항등.
- **통합 스모크**(20–80s 슬라이스): 60s pipeline + 10분 슬라이스 **양 경로 exit 0**, backward-compat(OFF) 보존, preprocess 적용·remap·payload 정상.
- **샘플 A/B**(20–80s 슬라이스, 윈도우 ref 144 tokens, **전 구성 rp1.2 적용됨** — `cohere.py` 三 경로 모두 `repetition_penalty=1.2`):

| config | applied | WER↓ | CER↓ | rep_ratio | Δ vs base |
|---|---|---|---|---|---|
| base | – | 0.688 | 0.532 | 0.000 | — |
| +GTCRN | gtcrn | 0.722 | 0.527 | 0.000 | **+0.035** ⚠️ |
| +WPE+GTCRN | wpe,gtcrn | 0.646 | 0.483 | 0.000 | −0.042 ✅ |
| +WPE+GTCRN+VAD | wpe,gtcrn,silero | **0.562** | **0.407** | 0.000 | **−0.125** ✅ |

**해석**: 풀스택이 base 대비 WER −12.5%p (개선). VAD 무음압축 기여 큼. **GTCRN 단독은 역효과(+0.035)** → WPE 와 결합 필수. `rep_ratio` 전 구성 0 = rp1.2 가 이미 반복 억제(VAD 의 anti-collapse 역할 흡수됨).
**한계**: 윈도우 경계로 절대 WER 부풀려짐(tok_ratio 0.59~0.74) → **신뢰 신호는 Δ**. 60초 슬라이스는 collapse 없는 깨끗한 구간 → 풀스택 진짜 가치는 83분 전체에서만 확정.

---

## 5. ⚠️ 알려진 이슈 / 함정

1. **`samples` 심볼릭 링크가 gitignore 안 됨** — `.gitignore` 의 `samples/`(슬래시) 패턴이 심볼릭 링크 `samples`(슬래시 없음)를 안 잡음. `git status` 에 `?? samples` 로 뜸. **절대 `git add samples` 하지 말 것.** 커밋 전 `git add` 시 명시적으로 파일 지정하거나, `.gitignore` 에 `/samples` 추가 권장.
2. **`output/` 는 worktree 에 없음**(gitignore) — 실행 전 `mkdir -p output` 필요(쉘 리다이렉트 실패 방지).
3. **`models/gtcrn/*.tar` gitignore됨** — vendored 모델 *코드*(`_vendor/gtcrn/`)는 커밋되지만 *체크포인트*는 아님. 새 환경마다 fetch(§1).
4. **GTCRN istft**: upstream 은 torch 1.11 기준 `return_complex=False`. 우리 torch 2.5 는 istft 가 complex 입력 요구 → `gtcrn_denoiser.py` 에서 `torch.view_as_complex()` 로 변환(적용 완료).
5. **torchaudio CUDA 정합**: 기본 pypi torchaudio 는 cu124 빌드라 torch(cu121)와 불일치 → `pyproject.toml [tool.uv.sources]` 에 `torchaudio = { index="pytorch-cu121" }` 고정(적용 완료). silero-vad 가 torchaudio 를 import 하므로 필수.
6. **단일 호출(`run.py` 무옵션)은 max_new_tokens=256** → 30초+ 입력 truncation. 평가는 반드시 `--pipeline` 또는 슬라이스 경로.

---

## 6. 남은 작업 (우선순위)

1. **🔴 풀스택 83분 전체 완주** — 로컬 팅김으로 미완. **Docker 환경**에서 `tools/run_long_slice10m.py "samples/ax...m4a" --dereverb --denoise --vad` 완주 → `score_frontend.py` 로 클로바 ref WER 측정. **rp1.2 baseline(메모리상 10분슬라이스 0.529) 대비 Δ 확정**이 목표.
2. **🟡 Dockerfile** — 미결정: **모델 bake vs 볼륨**. 권장 = **하이브리드**(Cohere 3.9G 볼륨 마운트, GTCRN 566KB·코드 bake, Silero 는 런타임 캐시/오프라인 주의). GPU 런타임(`--gpus all`), CUDA 12.1 base.
3. **🟡 feat/vad 커밋** — 아직 미커밋. `samples` 심볼릭 링크 제외하고 add. 커밋 메시지에 담당자/행번호 표현 금지(워크스페이스 규칙).
4. **🟢 GitHub private push** — VAD 완료 후(다른 세션 대기 항목).
5. **🟢 단계 격리 A/B(전체)** — +GTCRN / +WPE / +VAD 각 격리 측정으로 "GTCRN 단독 역효과"가 83분에서도 재현되는지 확인.

---

## 7. 의존성 추가분
`silero-vad>=5.1`(MIT, ~2MB, CPU) / `torchaudio>=2.5,<2.6`(**cu121 인덱스 고정**) / `einops>=0.7`(GTCRN 필요). 기존 `nara-wpe`(WPE) 재사용.

## 관련 문서
- 구현 플랜: `~/.claude/plans/zipenhancer-md-stateless-valiant.md`
- ZipEnhancer/ONNX 향후: `docs/denoiser-zipenhancer-plan.md`
- 청크 전략·rp1.2: 메모리 `project-stt-chunking-cohere`, `docs/2026-05-29-*.md`
