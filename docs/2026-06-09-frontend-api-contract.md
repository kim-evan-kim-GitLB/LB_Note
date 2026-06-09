# 프론트 ↔ 온프레미스 백엔드 API 계약 (2026-06-09 확정)

- 대상: meetscript-ai 프론트(별도 세션) ↔ 온프레미스 FastAPI 백엔드(`src/web/app.py`)
- 목적: **두 세션이 직접 만나지 않고 이 계약에서만 만난다.** 프론트 세션은 이 문서 하나만 보고 작업.
- 관련: [[meetscript-frontend-integration]], 요약 설계 `docs/2026-06-09-summarize-stage-design.md`
- Jira: WDLABD2411-531

## 0. 확정된 계약 결정 (2026-06-09)

| # | 항목 | 결정 | 변경 주체 |
|---|---|---|---|
| 1 | `summary` 타입 | **객체(구조체)** — string→object | **프론트**(types.ts) |
| 2 | `/api/ai/process` | **비동기**(jobId + 폴링) 유지 | **프론트**(폴링 추가) |
| 3 | `actionItems` | **리치 객체**(owner/anchor/evidence 포함) | **프론트**(ActionItem 확장 + `due`→`dueDate`) |
| 4 | `/api/ai/extract-actions` | **구현됨** — `string[]` 반환 | 백엔드(완료) |

→ **백엔드는 1·2·3을 이미 그 모양으로 제공**(추가 작업 없음). 4는 구현 완료. **프론트가 1·2·3에 맞춰 수정**하면 연동 완료.

## 1. 엔드포인트 흐름

### 1-1. POST /api/ai/process — 오디오 제출(비동기)
요청: `{ audioBase64, mimeType, participants, promptTemplate?, title? }`
응답(즉시): `{ "jobId": "<hex>", "status": "processing" }`

### 1-2. GET /api/ai/jobs/{jobId} — 잡 폴링 (프론트 신규 구현 필요)
- `{ "jobId", "status": "processing" }` — 진행 중(계속 폴링)
- `{ "jobId", "status": "done", "result": { summary, actionItems, transcript, duration } }` — 완료
- `{ "jobId", "status": "error", "error": "..." }` — 실패

> **프론트 변경(결정 2):** `processMeetingAudio` 가 지금은 `process` 응답을 바로 결과로 쓰는데,
> 이제 `{jobId}` 를 받고 **`GET /api/ai/jobs/{jobId}` 를 status가 done/error 될 때까지 폴링**(예: 2초 간격)
> → `result` 를 `Partial<Meeting>` 로 매핑. STT가 수 분 걸려 sync HTTP는 타임아웃 위험이라 폴링 채택.

### 1-3. POST /api/ai/extract-actions — 텍스트→액션(구현 완료)
요청: `{ "text": "..." }` → 응답: `string[]` (예: `["모델 확정", "보고서 작성"]`)
- raw text 입력이라 anchor/owner/evidence 없이 **액션 문구만** 평탄화. `WEB_EXTRACT_BACKEND=passthrough`면 `[]`.

## 2. process 결과(result) 실제 JSON

```jsonc
{
  "summary": {                              // 결정 1: 객체(string 아님)
    "schema_version": "sum-1.0",
    "prompt_version": "summarize-ko-1.0",
    "backend": "agent_cli",
    "meta": { "datetime": "", "department": "", "attendees": [],
              "subject": "EYEL-S3000ABR Demo시연", "author": "" },
    "agenda_index": [                        // 안건 목록 테이블
      { "no": 1, "title": "3D SVM 뷰 검토", "summary": "11개 뷰, 애니메이션 추가" }
    ],
    "agenda": [                              // 안건별 상세(no 로 agenda_index 와 조인)
      {
        "no": 1, "title": "3D SVM 뷰 검토", "time_range": "00:12 ~ 05:21",
        "evidence_seg_ids": [0, 1],
        "points":    [{ "text": "3D SVM 첫 적용 사례.", "anchor": "00:12", "evidence_seg_ids": [0] }],
        "decisions": [{ "text": "애니메이션 추가하기로 결정.", "anchor": "01:35", "evidence_seg_ids": [1] }],
        "issues":    []
      }
    ]
  },
  "actionItems": [                           // 결정 3: 리치 객체
    { "text": "플레이어 충돌 해결", "owner": "SW2팀", "due": "최대한 빠르게",
      "anchor": "01:35", "evidence_seg_ids": [1], "flag": null }
  ],
  "transcript": [
    { "speakerId": "", "text": "3D SVM 첫 적용 사례", "timestamp": "00:12" }
  ],
  "duration": "5:21"
}
```

**요약 off/실패 시** `summary` 는 빈 구조체(타입 일관): `agenda_index:[]`, `agenda:[]`, `meta` 빈 값.
**owner** 는 본문에 팀/부서가 명시될 때만 채움(아니면 `null`). **anchor** 는 `MM:SS`(없으면 `null`).

## 3. 프론트(types.ts) 채택할 타입 (결정 1·3)

```ts
// summary: string → 아래 구조체
export interface SummaryItem { text: string; anchor: string | null; evidence_seg_ids: number[]; }
export interface AgendaBlock {
  no: number; title: string; time_range: string | null; evidence_seg_ids: number[];
  points: SummaryItem[]; decisions: SummaryItem[]; issues: SummaryItem[];
}
export interface AgendaIndexEntry { no: number; title: string; summary: string; }
export interface MeetingMeta {
  datetime: string; department: string; attendees: string[]; subject: string; author: string;
}
export interface MeetingSummary {
  schema_version: string; prompt_version: string; backend: string;
  meta: MeetingMeta; agenda_index: AgendaIndexEntry[]; agenda: AgendaBlock[];
}

// ActionItem: owner/anchor/evidence 수용 + due→dueDate 매핑
export interface ActionItem {
  id: string;
  text: string;
  status: 'new' | 'in-progress' | 'completed';
  meetingId: string;
  meetingTitle: string;
  dueDate?: string;        // 백엔드 `due` 를 매핑
  owner?: string | null;   // 신규: 팀/부서(없으면 null)
  anchor?: string | null;  // 신규: MM:SS(근거 시점)
  evidenceSegIds?: number[]; // 신규: 근거 segment(백엔드 evidence_seg_ids)
}

export interface Meeting {
  // ...
  summary: MeetingSummary;   // string → MeetingSummary
  actionItems: ActionItem[];
  // ...
}
```

**매핑 주의:** 백엔드 actionItems 의 `due`→프론트 `dueDate`, `evidence_seg_ids`→`evidenceSegIds`.
`id`/`status`/`meetingId`/`meetingTitle` 은 백엔드가 모름(회의 저장 시 프론트가 생성).

## 4. 변경 없는 부분 (그대로 OK)
- `transcript`: `{ speakerId, text, timestamp }` 그대로(speakerId 는 화자분리 미적용이라 `""`).
- `/api/meetings*`(SQLite 영속) 계약 무변경 — 프론트의 process→save 흐름 유지.

## 5. 백엔드 env (운영 토글)
- `WEB_CLEAN_BACKEND`(기본 passthrough) / `WEB_EXTRACT_BACKEND`(기본=clean) / `WEB_SUMMARIZE_BACKEND`(기본 off).
- 추출·요약을 켜려면 해당 env 를 실 백엔드(예 `agent_cli`)로. 모두 off면 transcript 만, summary/actionItems 는 빈 값.

## 6. 통합 순서
1. 프론트 세션: 위 §3 타입 채택 + §1-2 폴링 구현(mock JSON §2 로 개발 가능).
2. 백엔드 세션: 실 LLM(`agent_cli`)로 process E2E 1회 검증(요약/추출 실호출).
3. 합치고 end-to-end 확인.
