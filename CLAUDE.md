# CLAUDE.md

이 저장소에서 작업할 때 Claude Code 가 따라야 할 지침입니다.

## 출력 언어 규칙 (중요)

- **모든 응답·결과 보고·요약은 한글로 출력합니다.** 사용자가 영어를 명시적으로 요청한 경우만 예외.
- STT transcript, 평가 지표(WER/CER 등) 설명, 표·결론 요약 등 결과물은 한글로 작성합니다.
- 코드 주석은 기존 코드 스타일을 따릅니다(이 저장소는 한글 주석 사용).
- 점수 리포트(`output/score-*.md`)와 같은 산출물의 설명 텍스트도 한글로 작성합니다.

## 프로젝트 개요

Cohere `transcribe-03-2026` 모델 기반 한국어 STT 파이프라인.

- `run.py` — 진입점. 단일 transcribe 또는 `--pipeline` 통합 처리.
- `src/pipeline.py` — 음성 → 전처리 → 청킹 → STT → `text.json`/`transcript.md` 파이프라인.
  - **기본 청킹 = vad_chunk(energy)**: VAD 발화 경계 분할 + 배치 디코딩 + seam dedup (2026-06-04 메인 승격).
- `src/stt.py`, `src/backends/cohere.py` — Cohere 백엔드. `transcribe()`(단일 호출),
  `transcribe_array()`(청크 1개), `transcribe_arrays()`(VAD 분할 청크 배치 디코딩).
- `src/scoring.py` — WER/CER 계산 및 reference 포맷 자동 감지(AI Hub / Clova Note / plain txt).
- `src/preprocess.py`, `src/backends/` — WPE dereverb, GTCRN denoise, Silero VAD (opt-in, 기본 OFF).
- `tools/` — 일회성 실험·평가 스크립트.
- `answer/` — reference(정답) 텍스트. `samples/` — 입력 음성. `output/` — 산출물.

## 실행 환경 주의사항

- **가상환경 python 은 `sudo` 로 실행해야 합니다.** `.venv/bin/python` 은 `/root` 아래 uv 관리 인터프리터를 가리켜 일반 사용자(`evan`)는 직접 실행 불가.
  - 예: `sudo .venv/bin/python tools/single_call_ax_clova.py`
- GPU: CUDA 사용 가능(NVIDIA RTX PRO 6000 Blackwell). bf16 기본, OOM 시 int8 자동 폴백.
- 설정은 `.env` + `src/config.py` 로 관리. `COHERE_MODEL_PATH=/app/models/cohere-transcribe-03-2026`.

## STT 추출 방식

- **vad_chunk(energy) 모드 (기본·권장)**: `run.py --pipeline` — VAD(`energy`) 로 발화 경계를 찾아
  ≤`target_sec`(<35s) 청크로 분할 → 배치 디코딩 → seam dedup 병합. 컷이 항상 무음 경계라
  단어 절단이 없고, VRAM 4GB / RTFx 232 / 타임스탬프 제공. (WER 0.417 / CER 0.299, ax 음원 검증.)
  - 새 플래그: `--segmentation {vad,fixed}`(기본 vad), `--vad-backend {energy,silero}`(기본 energy),
    `--batch-size INT`(기본 8), `--target-sec FLOAT`(기본 30.0).
  - 설계 문서: `docs/2026-06-04-vad-chunk-pipeline.md`.
  - 주의: 위 분할 VAD 는 기존 `--vad`(Silero **무음압축** 전처리)와 별개다. 두 옵션은 직교적으로 동작한다.
- **고정 길이 슬라이스 모드 (하위호환)**: `--segmentation fixed --chunk-sec 60 --overlap-sec 10`
  — `chunk_sec`/`overlap_sec` 로 분할 후 merge. 기존 동작을 그대로 재현.
- **단일 호출(슬라이스 없음) 모드**: 전체 음성을 한 번의 `generate()` 로 처리. 모델 내부 `audio_chunk_index` 로 long-form 디코딩.
  - 참고 스크립트: `tools/single_call_ax_clova.py` (ax 회의 음성 + Clova reference 평가).
  - hallucination 반복 억제: `repetition_penalty=1.2` 적용.

## 평가 지표

- WER(단어 오류율) ↓, CER(문자 오류율) ↓ — `src/scoring.py`.
- repetition burst / repetition_ratio — long-form collapse(반복 hallucination) 정량화.
- RTFx ↑(실시간 대비 처리 속도), VRAM peak.
- reference 가 Clova STT 결과인 경우 ground truth 가 아니므로 **WER 절대값보다 패턴·구간 신호로 해석**합니다.
- reference 텍스트 로드 시 Clova Note 의 화자 헤더(`참석자 N MM:SS`)는 제거 후 정규화합니다.

## 모호성 처리 (중요)

- **요구사항·범위·위치가 모호하다고 판단되면 추측으로 진행하지 말고, 먼저 사용자에게 질문하거나 짧은 인터뷰를 진행한다.**
- 특히 다음은 진행 전에 반드시 확인한다:
  - 파일/디렉토리 **생성 위치** (예: docs 는 `/app/docs/` 아래 등 — 임의로 만들지 말 것)
  - 덮어쓰기·삭제 대상, 출력 파일명·포맷
  - 작업 범위(어디까지), 실행 대상 데이터/음원
- 합리적 기본값이 명백한 사소한 선택은 그대로 진행하되, **무엇을 가정했는지 응답에 명시**한다.
- 질문이 2개 이상이면 한 번에 모아서 묻는다(왕복 최소화).

## Git 커밋 시 Jira 태스크 연결 (중요)

- 커밋을 만들기 **전에** 해당 변경이 어떤 **Jira 태스크와 연관되는지 먼저 확인**한다(추측 금지).
- **태스크 탐색 순서:**
  1. 가능하면 **Jira MCP 서버나 API**를 활용해 현재 사용자에게 할당/관련된 태스크를 조회하고,
     후보를 제시해 어떤 태스크인지 사용자에게 질문한다.
  2. MCP/API로 **조회·추적이 안 되는** 경우(서버 미연결·권한 없음 등)에는 사용자에게 태스크 키
     (예: `PROJ-123`)를 **직접 입력받는다.**
  3. **연관 태스크가 없는 커밋**(문서·실험·잡일 등)도 정상 케이스로 상정한다 — 이때는 태스크 없이
     진행하되, "연관 태스크 없음"을 사용자에게 확인한 뒤 커밋한다.
- 확정된 태스크 키는 **커밋 메시지에 포함**한다(제목 접두 또는 풋터, 예: `[PROJ-123] ...`).
- 커밋은 사용자가 명시적으로 요청할 때만 수행한다(기본 정책 유지).

## Git 브랜치 규칙 — LB_Note (중요)

- 원격: `git@github.com:kim-evan-kim-GitLB/LB_Note.git` (origin, SSH 인증).
- **업데이트(커밋/푸시)는 항상 새 브랜치를 분기해서 진행한다. default 브랜치(main/master)에 직접 커밋·푸시 금지.**
- **브랜치명에는 반드시 `171` 을 포함한다** (현재 작업 서버가 171 서버이므로). 권장 형식: `171/<주제>` 또는 `feature/171-<주제>` (예: `feature/171-stt-vad-chunk`).
- 새 작업 브랜치는 원격 default 브랜치에서 분기(`git fetch origin` 후 `git checkout -b 171/... origin/<default>`).
- 푸시 후에는 PR(브랜치 → default)로 병합하는 흐름을 따른다.
- 커밋·푸시는 사용자가 명시적으로 요청할 때만 수행한다. (위 [[Jira 태스크 연결]] 규칙과 함께 적용)
