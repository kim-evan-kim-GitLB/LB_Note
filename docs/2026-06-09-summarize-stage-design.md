# 요약 스테이지 설계 — SummarizeStage (2026-06-09)

- 관련 Jira: WDLABD2411-531 (LB NOTE Phase 1)
- 선행 설계: `docs/2026-06-04-postprocess-pipeline-design.md`(§9 확장 로드맵 `summarize → agenda → action_items`)
- 자매 문서: `docs/2026-06-09-action-item-criteria.md`(액션아이템 추출/평가 기준)
- 상태: **설계 확정(인터뷰 4문항 반영) · 구현 전.** 코드/프롬프트/커밋은 별도 요청 시.

## 0. 확정된 결정 (2026-06-09)

인터뷰 4문항 + **실제 회의록 양식**(`samples/EYEL-S3000ABR 데모시연 회의록.pdf`, 회사 표준 포맷) 대조로 확정:

| 항목 | 결정 |
|---|---|
| 전체 구조 | **회사 표준 회의록 양식**을 목표 포맷으로 채택(헤더 → 안건 목록 → 상세 논의 → Action Item → 푸터) |
| 상세 논의 형태 | **하이브리드** — 안건별 **평문 불릿** + 끝에 `결정`·`이슈` 분리 태그(빈 값 허용) |
| 요약 단위 | **안건(주제)별 분할** + 상단 **안건 목록 인덱스 테이블**(전체 개관 역할) |
| 시점 표기 | 안건별 **시간 범위** `MM:SS ~ MM:SS`, 불릿 항목엔 `anchor`(시작 MM:SS) |
| 근거 표시 | 각 항목에 `evidence_seg_ids` + `anchor` 부착(LLM 불신, 호출부 결정적 산출) |
| LLM 백엔드 | **`WEB_SUMMARIZE_BACKEND`** 신규 env 로 정제·추출과 독립 |

> **인터뷰 vs 실제 양식 정합(2026-06-09):** 인터뷰에선 "3섹션(논의/결정/이슈)"을 골랐으나, 실제
> 회의록은 안건별 평문 불릿이라 **하이브리드**(불릿 + 결정/이슈만 분리)로 최종 확정. 안건 목록 테이블·
> 시간범위·헤더/푸터는 실제 양식을 그대로 채택.

> 화자분리(diarization)는 **범위 밖**. 요약문은 화자 귀속을 하지 않는다. (단 액션아이템 `owner` 는
> 실제 양식대로 **팀/부서**가 본문에 명시되면 채운다 — 자매 문서 §4 참조, 화자분리 불필요.)

### 실제 회의록 양식 구조 (목표 포맷)
```
회의록
[헤더]  회의 일시 · 주관 부서 · 참석자 · 주제
[안건 목록]  No. | 안건 | 주요 내용            ← 인덱스 테이블(개관)
[상세 논의 내용]  N 안건제목 (MM:SS ~ MM:SS)   ← 안건별 시간범위 + 평문 불릿
[Action Item]  담당(팀/부서) | Action Item | 기한
[푸터]  작성일 · 작성자
```
헤더·참석자·작성자 등 메타는 STT 로 못 얻는 필드라 **입력 메타(업로드 시 제공)로 채우거나 빈 값**
(graceful). 본 스테이지는 안건 목록·상세 논의 생성을 담당, Action Item 은 `extract` 스테이지 산출.

## 1. 목표와 범위

정제본(`text-{stem}.cleaned.json`, segment 단위 타임스탬프 보존)에서 **읽을 수 있는 회의 요약**을
산출한다. 액션아이템 추출(`extract`)과 **별개 스테이지**이며, 같은 계약·게이트·그라운딩 원칙을 재사용한다.

- **포함**: 안건 목록(인덱스) + 안건별 상세(논의 불릿 + 결정/이슈) 구조화 요약, 각 항목 근거(segment+anchor).
  (실제 회의록 양식엔 별도 헤드라인 문단이 없어 안건 목록 테이블이 개관 역할을 한다.)
- **제외**: 화자 귀속, 액션아이템(별 스테이지), transcript 원문 재생산, 근거 없는 내용 생성.

## 2. 핵심 원칙 (선행 설계 §2 계승)

LLM은 비결정적이라 전제하고, 안정성은 **모델 바깥**에서 잡는다:
1. **계약 우선**: 출력은 고정 스키마(아래 §4). 다운스트림(웹/회의록.md)은 항상 같은 구조.
2. **그라운딩 필수**: 모든 요약 항목은 `evidence_seg_ids`(≥1)를 인용한다. **근거 없는 항목 금지**
   = 할루시네이션 1차 차단. `anchor`(MM:SS)는 LLM 출력을 믿지 않고 **호출부가 결정적 산출**한다
   (액션아이템 anchor 와 동일 메커니즘, `seconds_to_timestamp(min(evidence start))`).
3. **검증·리페어 게이트**: 스키마 위반·근거 없는 항목·존재하지 않는 seg_id 인용 → 재시도→완화→
   부분반환+flag(graceful degrade, 멈춤 없음).
4. **버전 스탬프·캐싱**: `schema_version`·`prompt_version`·`backend` 기록. (segment집합+프롬프트버전+
   모델) 키 캐시로 재현.

## 3. 아키텍처 (스테이지 추가 한 개)

```
text-{stem}.cleaned.json (segments: id/start/end/cleaned)
   │
   ▼
[B] LLMBackend 어댑터 ── WEB_SUMMARIZE_BACKEND (정제·추출과 독립)
   ▼
[S] SummarizeStage ── 회의 1콜(전체 transcript_with_ids 주입), 안건 경계는 내용 기반 LLM 판단
   ▼
[D'] 요약 게이트 ── 스키마·근거존재·seg_id 유효성·길이 상한 → 실패시 부분반환+flag
   ▼
[anchor 결정적 산출] ── evidence_seg_ids → min(start) → MM:SS (호출부, LLM 불신)
   ▼
contract.summary (구조체) + 회의록.md 요약 섹션
```

- **단위 = 회의 1콜.** 추출과 동일하게 회의당 1회 호출(저비용). 안건 분할은 별도 로직 없이
  **프롬프트가 내용 기반으로 주제 블록을 나눈다**(화자분리 없음 → 화자 경계 못 씀).
- **입력**: `extract_schema.transcript_with_ids(segments)` 재사용 — 각 줄 `[id] 본문`. 추출과 같은
  주입 형식이라 seg_id 그라운딩 일관.

## 4. 출력 스키마 (계약 — types.ts 영향 있음)

현재 `contract.summary` 는 **빈 문자열 `""`**(미구현). 본 설계는 이를 **구조체**로 바꾼다(실제 양식 반영):

```jsonc
{
  "summary": {
    "schema_version": "sum-1.0",
    "prompt_version": "summarize-ko-1.0",
    "backend": "agent_cli",
    "meta": {                                  // STT 밖 메타(업로드 시 제공, 없으면 빈 값)
      "datetime": "2026-05-20 15:00", "department": "개발관리팀",
      "attendees": [], "subject": "", "author": ""
    },
    "agenda_index": [                          // ■ 안건 목록 (인덱스 테이블)
      { "no": 1, "title": "3D SVM 뷰 디자인 검토", "summary": "3D 모드 11개 뷰, 애니메이션 효과 추가" }
    ],
    "agenda": [                                // ■ 상세 논의 내용 (안건별)
      {
        "no": 1,
        "title": "3D SVM 뷰 디자인 검토",
        "time_range": "00:00 ~ 05:21",         // 안건 evidence 의 min(start) ~ max(end)
        "evidence_seg_ids": [15, 66, 80],
        "points": [                            // 평문 불릿(논의 본문, 섞어서)
          { "text": "현재 애니메이션 없음 → 뷰 전환 시 효과 추가 필요.",
            "anchor": "01:05", "evidence_seg_ids": [15, 66] }
        ],
        "decisions": [                         // 끝에 분리: 결정 사항(있을 때만)
          { "text": "심플한 구성으로 진행하기로 결정.", "anchor": "16:07", "evidence_seg_ids": [120] }
        ],
        "issues": [                            // 끝에 분리: 미결 이슈(있을 때만)
          { "text": "SVM 8ch/4ch 전환 미구현 상태.", "anchor": "02:30", "evidence_seg_ids": [88] }
        ]
      }
    ]
  }
}
```

### 스키마 규칙
- `meta`: 일시·주관부서·참석자·주제·작성자. **STT로 못 얻음 → 업로드 메타로 채우거나 빈 값**(graceful).
  `subject` 만 LLM 추론 허용(나머지 빈 값). **구현 현황(2026-06-09): 업로드 메타(title/participants)
  배선은 보류** — 현재 `subject` 외 빈 값 고정. 프론트 계약 합의 후 연결(§10).
- `agenda_index[]`: 안건 목록 테이블 한 줄(`no`/`title`/한 줄 `summary`). **`no` 가 상세 `agenda[]` 와의
  조인 키**(LLM 이 양쪽에 같은 no 부여, 호출부는 `_coerce_no` 로 보존). 그라운딩에서 **블록이 드롭되면
  같은 no 의 인덱스 줄도 제거**(목록↔상세 동기화, §7).
- `agenda[]`: 안건 블록. `time_range` = 해당 안건 evidence 의 `min(start) ~ max(end)`(MM:SS, 호출부 산출,
  end<start 노이즈는 hi≥lo 로 정규화). **단일 주제면 길이 1**.
- `points[]` = 평문 불릿(논의 본문). `decisions[]`·`issues[]` = **명확한 것만** 끝에 분리(없으면 빈 배열).
  과분류 금지 — 애매하면 `points` 에 둔다(하이브리드의 핵심: 불릿 우선, 결정/이슈만 변별).
- 모든 항목 `{text, anchor, evidence_seg_ids}`: `evidence_seg_ids` **필수(≥1)**, `anchor`=`min(start)`
  (LLM 출력 무시). `text` 는 한 문장(정보·고유명사 보존).
- **owner/화자 필드 없음**(요약문엔 화자 귀속 안 함). owner 는 액션아이템에만, 팀 명시 시(자매 문서 §4).

> **계약 영향(프론트 합의 필요):** `summary` 타입이 `string → object` 로 바뀐다. 이는
> `meetscript-frontend-integration` 의 미합의 계약 건과 같은 성격 → 프론트 `types.ts` 와 합의 후 연결.
> 요약 off(아래 §6)·실패 시 `summary` 는 **빈 구조체**로 둔다(타입 일관): `MeetingSummary.empty()` =
> `{schema_version, prompt_version, backend, meta(빈), agenda_index:[], agenda:[]}`.

## 5. 회의록.md 렌더링 (사람 가독)

```markdown
# 회의록
회의 일시: 2026-05-20 15:00 | 주관 부서: 개발관리팀 | 주제: …      ← meta

## 안건 목록                                                       ← agenda_index 테이블
| No. | 안건 | 주요 내용 |
|---|---|---|
| 1 | 3D SVM 뷰 디자인 검토 | 3D 모드 11개 뷰, 애니메이션 효과 추가 |

## 상세 논의 내용
### 1 3D SVM 뷰 디자인 검토 (00:00 ~ 05:21)                        ← time_range
- 현재 애니메이션 없음 → 뷰 전환 시 효과 추가 필요. (01:05)         ← points (불릿)
- SVM 8ch/4ch 전환 가능하나 현재 미구현. (02:30)
  ▸ 결정: 심플한 구성으로 진행. (16:07)                            ← decisions(있을 때만)
  ▸ 이슈: SVM 8ch/4ch 전환 미구현. (02:30)                         ← issues(있을 때만)

작성일: … | 작성자: …                                              ← meta(푸터)
```

## 5.1 출력 형식 정책 — JSON 정본 + MD 렌더 (TXT 보조)

산출물 저장 형식은 **하나를 고르는 문제가 아니라 역할 분리**다. 회의록은 *기계가 다루고 + 사람도
읽는* 산출물이라, **JSON을 단일 진실원(SSOT)으로 두고 MD를 거기서 파생 렌더**한다.

| 형식 | 역할 | 근거 |
|---|---|---|
| **JSON** | **정본(SSOT)** — `text.json`/`cleaned.json`/`actionitems.json`/web contract(`summary` 구조체) | 구조화·필드쿼리·다운스트림(웹·Jira·DB) 계약·스키마검증·회귀채점·**근거(evidence_seg_ids)·anchor·버전·flag 무손실**·부분갱신 |
| **MD** | **파생 뷰** — `transcript.md`/회의록.md | 사람 가독·배포·인쇄·복붙. JSON에서 렌더(§5). 정본 아님 |
| **TXT** | **보조** — STT 원시 transcript·로그·grep 대상 | 최소 의존·범용. 구조·메타 소실이라 회의록 정본으로 부적합 |

### 원칙
1. **정본은 JSON 하나.** MD를 정본 삼으면 evidence/타임스탬프/owner를 구조적으로 못 들고, 검증·
   부분갱신·DB 연동이 깨진다. MD/TXT 는 **항상 JSON에서 생성**(역방향 금지).
2. **DB 저장도 JSON.** `src/web/store.py` 는 SQLite `data` 컬럼에 Meeting JSON 을 통째로 보관 →
   이 정책과 일치. 필드 쿼리 수요가 생기면 **인덱스 컬럼만 추가**(정본은 여전히 JSON blob).
3. **TXT 는 회의록 산출물의 정본으로 쓰지 않는다**(근거·메타 소실). 원시 입력·로그용으로만.

## 6. 백엔드·운영 모드

- **`WEB_SUMMARIZE_BACKEND`**(신규): 정제(`WEB_CLEAN_BACKEND`)·추출(`WEB_EXTRACT_BACKEND`)과 **독립**.
- **폴백 정책: 미지정 시 요약 off**(`passthrough` 취급 → 빈 구조체 반환). 즉 "정제=passthrough인데
  요약만 켜기"는 이 env 를 **명시할 때만** 동작한다(정제 백엔드를 자동으로 따라가지 않음 — 의도된 독립).
- 비용: 회의당 1콜이라 클라우드(`agent_cli`)도 ≈$0.06 수준(추출과 동급). 나중에 로컬 Ollama
  스텁 구현 시 env 값만 바꾸면 됨(`get_llm_backend(name)` 팩토리, backend-agnostic).
- passthrough/비-JSON 백엔드 출력은 추출과 동일하게 **빈 결과로 방어**(파싱 실패 → 빈 구조체).

## 7. 검증·게이트 [D']

1. **스키마 검증** — 필수 필드/타입 위반 → 오류 첨부 재요청(최대 N회).
2. **근거 존재** — 모든 섹션 항목에 `evidence_seg_ids` ≥1. 없으면 그 항목 드롭+flag.
3. **seg_id 유효성** — 인용 id 가 실제 입력 segment 집합에 존재해야 함(환각 인용 차단). 존재하지
   않는 id 는 제거, 결과로 evidence 가 비면 항목 드롭. evidence 중복은 제거(순서 보존).
4. **블록·인덱스 동기화** — 근거 0 인 안건 블록은 드롭하고, **같은 `no` 의 `agenda_index` 줄도 제거**
   (목록↔상세 불일치 방지). `ground_summary` 가 수행.
5. **길이 상한** — 섹션 항목 수는 회의 규모 비례(예: 안건당 섹션별 ≤ N, 잠정값은 1차 측정 후 확정).
   과도 분해 금지(한 결정을 여러 항목으로 쪼개지 말 것).
6. 요약 백엔드 예외 시 **회의 전체를 죽이지 않고 빈 요약으로 graceful degrade**(정제·추출·transcript 보존).
7. 최종 실패 → 해당 부분만 빈 배열 + run 관측성에 flag 기록(전체 멈춤 없음).

## 8. 품질 게이트 — 요약을 어떻게 판정하나

WER/CER 부적합(요약은 원문과 표면형이 다름). 대신:

| 지표 | 정의 | 잠정 합격선 |
|---|---|---|
| 근거 충족률 | 섹션 항목 중 유효 `evidence_seg_ids` 보유 비율 | 100% |
| 인용 유효율 | 인용 seg_id 가 입력에 존재하는 비율 | 100% |
| 결정/이슈 회수 | 사람이 만든 정답 결정·이슈 목록 대비 회수율(키워드 그룹 매칭, 추출 평가 방식 재사용) | ≥ 0.8 (착수 기본값) |
| 사실 보존 | 표본 항목이 원문 사실을 누락/추가 없이 반영(사람 또는 의미검사) | ≥ 95% |
| 사람 수용 평가 | N=20 안건 항목 표본 가독성·정확성 2점 척도 | 합격 ≥ 90% |

- 평가셋: ax 회의 정제본에서 정답 요약(결정·이슈)을 사람이 1회 라벨 → 회귀 고정셋 보관.
  (액션아이템 정답셋 `eval/gold_actionitems.json` 과 같은 keyword-그룹 결정적 채점 방식 재사용 →
  자매 문서 참조.)
- 잠정 수치는 착수 기본값, 1차 측정 후 사용자와 확정.

## 9. 디렉터리/파일 (구현 현황 2026-06-09 — ✅ 구현됨 / ⬜ 미구현)

```
src/postprocess/
  summarize_schema.py      # ✅ SummaryItem/AgendaBlock/AgendaIndexEntry/MeetingMeta/MeetingSummary
                           #    + ground_summary(결정적 anchor/time_range·근거검증·인덱스 동기화)
  stages/summarize.py      # ✅ SummarizeStage(회의 1콜, transcript_with_ids 주입, 견고 파싱→스키마)
  validate.py              # ⬜ 게이트는 현재 ground_summary 에 내장(별 모듈 분리는 후속)
prompts/summarize.ko.md    # ✅ summarize-ko-1.0(버전 명시, 인젝션 격리, 하이브리드/그라운딩 규칙)
src/postprocess/web_contract.py  # ✅ summary str→구조체(_summary_or_empty). 회의록.md 렌더는 ⬜
src/web/{app,service}.py   # ✅ WEB_SUMMARIZE_BACKEND 게이팅, summarize_meeting, graceful degrade
eval/gold_summary.json     # ⬜ 결정·이슈 정답셋(회귀 고정) — 평가 단계에서
tests/test_summarize.py    # ✅ 14 케이스(결정성·근거드롭·인덱스동기화·코드펜스·타입계약·역순가드)
```

## 10. 미해결 / 사용자 확정 필요

- §4 안건 구조 채택안: **안건별 → 각 안건에 3섹션**(본 설계). 대안 = "전체 3섹션 + 항목에 안건 태그".
  본 설계는 인터뷰 두 답(섹션구조 + 안건별)을 **둘 다** 만족하는 안으로 택함 — 다르면 교체.
- §7 길이 상한(섹션 항목 수) 잠정값 → 1차 측정 후 확정.
- §8 결정/이슈 정답셋(`eval/gold_summary.json`) 라벨 주체·시점 + 실 LLM(agent_cli) E2E 회수율 측정.
- **meta 업로드 배선(보류)**: 프론트가 보내는 `title`/`participants` 를 `meta.subject`/`meta.attendees` 로
  연결하는 경로 미구현(현재 `subject` 만 LLM 추론). `participants` 스키마가 프론트 계약 미합의 사항이라
  보류 — 합의 후 `process_audio_to_contract(meta=...)` 파라미터로 주입.
- 프론트 `types.ts` 의 `summary: string → object` 마이그레이션 합의(프론트 미연결 상태라 선행 합의 권장).
- 요약 백엔드 예외 graceful degrade 를 **추출에도 동일 적용**할지(현재 요약만 try/except) 정책 통일.
