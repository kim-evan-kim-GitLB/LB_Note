# lb-note 모노레포 모듈화 설계

## Context

`lb-note` 는 현재 `src/` 하나에 STT 백엔드(Cohere) · 오디오 IO · 청킹 · 평가가 모인 모놀리식 Python 프로젝트다. 모듈은 비교적 잘 분리돼 있지만 (`backends/base.py` 추상화, `audio_io.py` / `chunker.py` / `scoring.py` 단일 책임), 다음 6 가지가 부재해서 회의록 자동화 도구로 발전시키려면 구조 확장이 필요하다:

1. LLM 기반 보정·요약·액션 아이템 도출
2. UI (Frontend)
3. Backend API
4. DB / 영속화
5. 인증·외부 시스템 연동
6. 음성 전처리 심화(VAD/화자분리)

또한 `src/pipeline.py:77 run_pipeline()` 이 IO·payload 빌드·scoring·파일 쓰기를 한 함수에 묶고 있고, `tools/` 에는 평가·합성 스크립트 5개가 산재해서 그대로 확장하면 STT 코어와 웹 계층이 결합된다.

목표는 **단일 git repo 안에서 STT 코어 라이브러리 / LLM 라이브러리 / API / Worker / Web / 외부 연동을 패키지 단위로 분리**하여, 추후 50명+ 사내 서비스로 수평 확장 가능한 형태로 만드는 것이다. 단, **Phase 1 범위는 STT+LLM 코어 완성에만 한정**한다 — Frontend(Next.js)와 Backend API(FastAPI)는 Phase 2 본격 구현이며, Phase 1에서는 골격 폴더와 ABC 자리만 마련한다.

## 확정된 의사결정

| # | 항목 | 결정 |
|---|---|---|
| 1 | 배포 형태 | 단계적 (PoC 1~5명 → 사내 50명+) |
| 2 | 모델 호스팅 | STT 로컬 GPU + LLM 외부 API (OpenAI/Anthropic) |
| 3 | 리포 구조 | 모노레포 + 패키지 분리 (uv workspace, PyPI 미배포) |
| 4 | Frontend | Next.js + TypeScript (App Router) — **Phase 2 본격 구현**, Google AI Studio 디자인 반영 |
| 5 | 화자 분리 | Phase 2 (pyannote-audio), Phase 1은 NullDiarizer 훅 |
| 6 | 외부 연동 | Phase 2: Jira / Confluence / Notion / Slack — PoC는 단독 도구 |
| 7 | 음성 워크플로 | 배치 (업로드 → 처리). 실시간 STT는 추후 |
| 8 | DB | 처음부터 PostgreSQL (SQLAlchemy + Alembic) |
| 9 | 산출물 | 전체 전사본(타임스탬프) / 행정 요약 / 액션아이템(담당자·기한) / 결정사항 · 주제별 소제목 |
| 10 | 데이터 보관 | 음성 원본 처리 후 삭제, 전사본·요약만 보관 |
| 11 | 인증 | PoC 무인증, FastAPI `Depends` 훅 자리만 마련 (추후 Google SSO/JWT) |
| 12 | 실시간 STT | Phase 1 범위 밖 |

## 최종 디렉토리 구조

```
lb-note/
├── packages/
│   ├── lb-note-core/        # STT 백엔드 · 오디오 · 청킹 · 평가
│   ├── lb-note-llm/         # LLM 클라이언트 · 프롬프트 · 요약 전략
│   ├── lb-note-db/          # SQLAlchemy 모델 · Alembic
│   ├── lb-note-api/         # FastAPI (업로드/조회/잡 상태)
│   ├── lb-note-worker/      # 비동기 실행 (PoC: BackgroundTasks, 추후 Celery)
│   ├── lb-note-integrations/ # Jira/Confluence/Notion/Slack 어댑터 (Phase 2)
│   └── lb-note-web/         # Next.js + TS (App Router)
├── apps/
│   ├── cli/                 # run.py 후신 — packages 호출
│   └── tools/               # 평가·합성 스크립트 보존
├── infra/
│   ├── docker/              # api/worker/web Dockerfile
│   ├── compose/             # PoC docker-compose
│   └── k8s/                 # Phase 3 자리
├── models/                  # gitignored (cohere-transcribe-03-2026/)
├── samples/
├── docs/
│   ├── architecture.md
│   ├── data-flow.md
│   └── decisions/           # ADR
├── scripts/                 # dev_up.sh, db_reset.sh, smoke.sh
├── tests/                   # multi-package e2e
├── pyproject.toml           # uv workspace 루트
└── README.md
```

**의존 방향 (순환 금지):**

```
web ──HTTP──▶ api ──▶ worker ──▶ core
                │         │        │
                ▼         ▼        ▼
               db        llm   (backends)
                ▲
               integrations (Phase 2)
```

`core` 는 다른 `lb-note-*` 에 의존하지 않는다(라이브러리 성격).

## 현재 파일 → 새 위치 매핑

| 현재 경로 | 이동 후 |
|---|---|
| `src/audio_io.py` | `packages/lb-note-core/src/lb_note_core/audio_io.py` |
| `src/chunker.py` | `packages/lb-note-core/src/lb_note_core/chunker.py` |
| `src/types.py` | `packages/lb-note-core/src/lb_note_core/types.py` |
| `src/config.py` | `packages/lb-note-core/src/lb_note_core/config.py` (하드코드 제거) |
| `src/stt.py` | `packages/lb-note-core/src/lb_note_core/backends/registry.py` |
| `src/backends/base.py` | `packages/lb-note-core/src/lb_note_core/backends/base.py` |
| `src/backends/cohere.py` | `packages/lb-note-core/src/lb_note_core/backends/cohere.py` |
| `src/scoring.py` | `packages/lb-note-core/src/lb_note_core/eval/scoring.py` |
| `src/reference.py` | `packages/lb-note-core/src/lb_note_core/eval/reference.py` |
| `src/pipeline.py` | `packages/lb-note-core/src/lb_note_core/pipeline.py` (리팩토링) |
| `src/evaluate.py` | `apps/tools/evaluate_batch.py` |
| `src/_smoke.py` | `packages/lb-note-core/tests/test_smoke.py` (pytest화) |
| `run.py` | `apps/cli/lb_note_cli/__main__.py` |
| `tools/*.py` | `apps/tools/*.py` 그대로 |

## 핵심 인터페이스

`packages/lb-note-core/src/lb_note_core/interfaces.py` 에 단계별 Protocol 정의:

```python
class Preprocessor(Protocol):    # load + normalize (+ Phase 2 VAD/노이즈)
class Chunker(Protocol):         # split -> list[AudioChunk]
class STTBackend(Protocol):      # transcribe_array, vram_peak_mb
class Aligner(Protocol):         # overlap dedupe + timestamp
class Diarizer(Protocol):        # Phase 1: NullDiarizer no-op
class LLMSummarizer(Protocol):   # summarize_chunk / merge / extract_actions / extract_decisions
```

`pipeline.py` 는 `TranscriptionPipeline` 클래스로 재구성. 외부 어설션·파일 IO·payload JSON 생성 책임을 코어 바깥(`apps/cli`, `lb-note-api`)으로 이관하여 **코어는 데이터 객체(`PipelineResult`)만 반환**한다.

## DB 스키마 핵심 엔티티

```
Meeting(id, title, organizer_id, scheduled_at, location, timestamps)
AudioJob(id, meeting_id, original_filename, mime, duration_sec,
         status[pending|preprocessing|stt|summarizing|done|failed],
         backend, error, started_at, finished_at, vram_peak_mb, rtfx)
Transcript(id, meeting_id, full_text, language)
TranscriptSegment(id, transcript_id, idx, start_sec, end_sec, text,
                  speaker NULL, confidence NULL)        # Phase 2 화자
Summary(id, meeting_id, exec_summary_md, llm_model, prompt_version)
Topic(id, summary_id, idx, title, body_md)
Decision(id, meeting_id, idx, statement, rationale, source_segment_id NULL)
ActionItem(id, meeting_id, idx, description, assignee, due_date NULL,
           source_segment_id NULL, status[open|done|cancelled])
User(id, email, name, sso_subject NULL)                 # PoC: 시드 1명
IntegrationExport(id, meeting_id, target, external_ref, exported_at) # Phase 2
```

`AudioJob` 은 음성 원본을 저장하지 않고 메타·상태만 보관 — 처리 완료 시 임시파일 삭제 (결정 #10).

## 데이터 흐름

**Phase 1 (CLI):**

```
$ python -m lb_note_cli <audio_file>
  → core.audio_io.load_audio()        # 다중 포맷 디코드
  → core.chunker.split()              # 60s + 10s overlap
  → core.backends.cohere.transcribe() # 로컬 GPU STT
  → core.aligner.merge()              # overlap dedupe
  → llm.summarizer.summarize_chunk(text) for each chunk
  → llm.summarizer.merge()            # MapReduce 통합
  → llm.summarizer.extract_actions() / extract_decisions()
  → 출력: output/<timestamp>/{transcript.json, summary.md, actions.json, decisions.json}
```

음성 원본은 CLI 종료 후 처리하지 않음 (사용자 파일 그대로). DB 저장은 Phase 2에서.

**Phase 2 (API + Web):**

```
Browser ──POST /uploads (multipart) ──▶ api
   api: tempfile 저장, AudioJob(status='pending') 생성,
        BackgroundTasks.add_task(worker.transcribe_job, job_id) → 202 응답
   worker: preprocessing → stt → core.pipeline.run() → Transcript/Segments 저장
         → summarizing → llm.summarizer → Summary/Topic/Decision/ActionItem 저장
         → os.unlink(tmp) → status='done'
Browser ──GET /jobs/{id} (polling) ──▶ status, progress
        ──GET /meetings/{id} (status==done) ──▶ 전사본+요약+액션+결정
```

Phase 3 전환점은 단 한 곳: `BackgroundTasks.add_task` → `celery_app.send_task`. core/llm/db 무수정.

## Phase 1에서 의도적으로 안 만드는 것

| 구성요소 | 자리만 마련 | Phase 1 대체 |
|---|---|---|
| **Frontend (Next.js)** | `packages/lb-note-web/` 빈 폴더 + README | 없음. Phase 2에서 본격 구현 |
| **Backend API (FastAPI)** | `packages/lb-note-api/` 빈 폴더 + Protocol | CLI (`python -m lb_note_cli`) |
| **Worker** | `packages/lb-note-worker/` 빈 폴더 | CLI 내부 동기 실행 |
| **DB 운영 연동** | 스키마 + Alembic 마이그레이션만 | CLI 결과는 파일 출력 (JSON/MD) |
| Celery / Redis | `worker/celery_app.py` 빈 스텁 | — |
| 인증 | — | Phase 1 미적용 |
| 화자분리 | `core/diarization/null.py` | `NullDiarizer` 통과 |
| Integrations | 패키지 + ABC 만 | 미연동 |
| WebSocket 실시간 | — | — |
| Multi-tenant | — | 단일 조직 |
| k8s | 빈 폴더 | — |

## 마일스톤

**Phase 1 (코어 PoC — STT + LLM, 4~6주)**

범위: CLI / 라이브러리 레벨에서 회의록 자동화 파이프라인 완성. 웹/백엔드는 **자리만 마련** (빈 패키지 폴더 + Protocol/ABC 정의), 실제 구현 안 함.

- 모노레포 골격 + uv workspace + §3 파일 마이그레이션
- `lb-note-core`: 인터페이스 추출 + `TranscriptionPipeline` 리팩토링 (현 `pipeline.py` 분해)
- `lb-note-llm`: MapReduce 요약기 (OpenAI/Anthropic 클라이언트 둘 다) + 산출물 4종 (전사본/요약/액션/결정) Pydantic 스키마
- `lb-note-db`: 스키마 정의 + Alembic init (마이그레이션만, 실제 운영 DB 연동은 Phase 2)
- `apps/cli`: `python -m lb_note_cli <audio>` 로 끝~끝 실행 → JSON/Markdown 산출
- 빈 패키지 자리: `lb-note-api/`, `lb-note-worker/`, `lb-note-web/`, `lb-note-integrations/` (pyproject + README 만, 코드 없음)

**Phase 2 (Frontend + Backend 본격 구현 + 화자 분리 + 외부 연동, 8~10주)**

- `lb-note-api` (FastAPI): uploads / meetings / jobs 라우터 + BackgroundTasks 워커 + `api/deps.py` 인증 훅 (no-op으로 시작)
- `lb-note-worker`: 비동기 STT+LLM 실행, AudioJob 상태 머신
- `lb-note-web` (Next.js + TS): 업로드 + 결과 뷰어 + 진행률 폴링 (Google AI Studio 디자인 적용)
- `PyannoteDiarizer` 구현 → `TranscriptSegment.speaker` 채움
- `lb-note-integrations`: Jira/Confluence/Notion/Slack 어댑터
- Google SSO + JWT (`api/deps.py` 실구현)
- docker-compose: postgres + api + worker + web 풀스택 셋업

**Phase 3 (50명 운영 스케일, 8주+)**

- BackgroundTasks → Celery+Redis, api/worker Dockerfile 분리
- GPU 워커 수평 스케일
- k8s + 관측(OTel) + audit log
- 멀티 조직 (`organization_id` 컬럼 추가)

## Critical Files (Phase 1에서 손대는 파일)

이동·리팩토링:
- `/home/evan/Claude_workspace/lb-note/src/pipeline.py` — `TranscriptionPipeline` 클래스로 분해, payload/JSON 생성은 CLI로 이관
- `/home/evan/Claude_workspace/lb-note/src/backends/base.py` — Protocol 승격
- `/home/evan/Claude_workspace/lb-note/src/backends/cohere.py` — import 경로 수정
- `/home/evan/Claude_workspace/lb-note/src/chunker.py` — 위치 이동
- `/home/evan/Claude_workspace/lb-note/src/audio_io.py` — 위치 이동
- `/home/evan/Claude_workspace/lb-note/src/config.py` — `_ARCHIVE_PROJECT` 하드코드 제거, samples 경로 일반화
- `/home/evan/Claude_workspace/lb-note/run.py` → `apps/cli/lb_note_cli/__main__.py`

신규 작성 (Phase 1 범위만):
- 루트 `pyproject.toml` (uv workspace)
- `packages/lb-note-core/{pyproject.toml, src/lb_note_core/interfaces.py}`
- `packages/lb-note-llm/{pyproject.toml, src/lb_note_llm/{clients,prompts,schemas.py,summarizer.py}}`
- `packages/lb-note-db/{pyproject.toml, src/lb_note_db/models/, alembic/}` — 스키마·마이그레이션 정의만
- `packages/{lb-note-api,lb-note-worker,lb-note-web,lb-note-integrations}/` — 빈 폴더 + README + pyproject (Phase 2 자리)

## 검증 방법 (Phase 1 acceptance)

Phase 1 은 **CLI / 라이브러리 레벨 검증만 수행**. 풀스택 e2e(docker-compose, curl, Playwright)는 Phase 2 acceptance로 미룬다.

```bash
# 1) uv workspace 동기화 + 패키지 임포트 정합성
uv sync
uv run python -c "import lb_note_core, lb_note_llm, lb_note_db; print('ok')"

# 2) 단위 테스트 (스모크 + 백엔드 로드 + LLM mock)
uv run pytest packages/lb-note-core packages/lb-note-llm -q

# 3) CLI 끝~끝 실행 (현 run.py 와 동등 + LLM 산출물 4종 추가)
uv run python -m lb_note_cli samples/sample-2m.wav \
    --backend cohere --language Korean --out output/

# 산출물 검증
test -f output/latest/transcript.json   # STT 결과 (타임스탬프 포함)
test -f output/latest/summary.md        # 행정 요약
test -f output/latest/actions.json      # 액션 아이템 (담당자/기한 필드)
test -f output/latest/decisions.json    # 결정사항 + 주제별 소제목

# 4) DB 스키마 마이그레이션 (Phase 2 준비)
uv run alembic -c packages/lb-note-db/alembic.ini upgrade head
# → postgres 인스턴스에 §4 모든 테이블 생성 확인
```

위 4단계 통과 = Phase 1 완료. **API/Web/Worker 동작 검증은 Phase 2 acceptance.**
