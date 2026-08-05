"""[O] 다중 agent core — 라우터 + 병렬 전문 agent + critic 1패스 (2026-07-30 결정).

설계: docs/2026-07-30-영어환각-언어게이트-설계.md §2. 회의 1건을 아래 순서로 처리한다.

    Stage 0  언어 게이트(결정적)          language_gate.partition
    Stage 1  프로파일 + 라우팅(결정적)     meeting_profile.profile_meeting / route
    Stage 2  전문 agent **병렬** 실행      요약(창별) ∥ 액션(창별)
    Stage 3  병합(reduce, 장시간만) + 검증(critic 1패스)
    Stage 4  결정적 적용·그라운딩          판정 적용 → ground_summary → anchor 산출

설계 원칙:
- **케이스 판정은 LLM 이 하지 않는다**(Stage 0·1 은 산술·정규식만) → 재현성 + 콜 수 불변.
- **모든 LLM 콜은 병렬**이다. 시간 예산이 "단일 콜의 2배 이내"라 순차 실행은 불가.
  장시간 회의는 창을 나눠 병렬로 돌기 때문에 오히려 단일 콜보다 빨라진다.
- **판정과 적용을 분리**한다. critic 은 판정만, 적용(드롭·플래그·보강)은 이 모듈이 결정적으로.
- **어느 단계가 실패해도 회의를 죽이지 않는다**(빈 결과로 degrade). 요약 실패 ≠ 전사 유실.

병렬 실행 주의: 자격증명(agent_cli._active_credential)과 취소 이벤트는 ContextVar 라 새 스레드에
자동 전파되지 않는다. 그래서 `contextvars.copy_context()` 사본을 워커마다 만들어 그 안에서
호출한다 — 사본이 아니면 "cannot enter context: already entered" 로 깨진다.
"""
from __future__ import annotations

import contextvars
import traceback
from concurrent.futures import ThreadPoolExecutor

from src import config
from src.cancellation import OperationCancelled
from src.postprocess import language_gate, meeting_profile
from src.postprocess.backends import get_llm_backend
from src.postprocess.backends.agent_cli import AgentCLIAuthError
from src.postprocess.extract_schema import seconds_to_timestamp, transcript_with_ids
from src.postprocess.stages.critic import DROP, FLAG, CriticResult, CriticStage
from src.postprocess.stages.extract import ExtractStage
from src.postprocess.stages.localize import LocalizeStage
from src.postprocess.stages.reduce import ReduceStage
from src.postprocess.stages.summarize import SummarizeStage
from src.postprocess.summarize_schema import MeetingSummary, ground_summary
from src.postprocess.validate import FLAG_REVIEW
from src.postprocess.web_contract import _action_items_from_payload

# case 별 프롬프트 보강 지시문. 프롬프트 파일(자산)은 그대로 두고 여기서 상황별 문장만 덧붙인다
# — 케이스 조합이 늘어도 프롬프트 파일이 폭발하지 않는다.
_DIRECTIVE_CONSERVATIVE = (
    "### 추가 지시(보수 모드 — 전사 품질 저하 회의)\n"
    "이 회의의 전사 품질이 낮다(오인식·반복·비한국어 비중 높음). 근거가 약하면 **항목을 만들지 말고 "
    "비워라.** 빈 요약·빈 액션은 정상 결과다. 추측으로 문장을 완성하지 마라."
)
_DIRECTIVE_STRICT_OWNER = (
    "### 추가 지시(다주제·다부서 회의)\n"
    "주제가 여러 갈래이고 관련 팀도 여럿이다. 안건을 **주제 단위로 확실히 분리**하고, 서로 다른 "
    "주제를 한 안건에 섞지 마라. owner 는 **본문에 문자 그대로 명시된 경우만** 채운다"
    "(본문 앵커 추론 금지 — owner_source='inferred' 를 쓰지 마라). 확실치 않으면 owner=null."
)
# 병렬 워커에서 삼키지 않고 상위로 올릴 예외 — 인증 만료·취소는 사용자 대응이 필요한 신호다.
_PROPAGATE = (AgentCLIAuthError, OperationCancelled)

_DIRECTIVE_STRICT_CRITIC = (
    "### 추가 지시(환각 위험 회의)\n"
    "이 회의에는 STT 환각으로 의심되는 구간이 이미 탐지됐다. 판정을 **엄격히** 하라 — 근거를 읽어도 "
    "그 내용이 분명히 보이지 않으면 keep 하지 말고 drop 또는 flag 로 내려라."
)


# 삼켜진 실패 카운터 — run_meeting_core 1회 실행 범위.
# 여기서 세는 실패들은 전부 "회의 전체를 죽이지 않기 위해" 의도적으로 흡수하는 것들이다.
# 그런데 흡수만 하고 흔적을 안 남기면 산출이 빈약할 때 원인을 알 수 없다(실제 사고 사례).
# ContextVar 를 쓰는 이유: _run_parallel 이 contextvars.copy_context() 로 워커를 돌리므로
# 워커에서도 같은 dict 객체가 보이고, 회의 두 건이 동시 실행돼도 서로 섞이지 않는다.
_FAILURES: contextvars.ContextVar[dict] = contextvars.ContextVar("core_failures")


def _count_failure(kind: str) -> None:
    try:
        _FAILURES.get()[kind] = _FAILURES.get().get(kind, 0) + 1
    except LookupError:
        pass  # run_meeting_core 밖에서 호출된 경우(도구·테스트) — 세지 않는다


def _directives(plan: meeting_profile.Plan, *, for_critic: bool = False) -> str:
    """Plan → 프롬프트에 덧붙일 지시문(없으면 빈 문자열)."""
    parts: list[str] = []
    if plan.conservative:
        parts.append(_DIRECTIVE_CONSERVATIVE)
    if plan.strict_owner:
        parts.append(_DIRECTIVE_STRICT_OWNER)
    if for_critic and plan.strict_critic:
        parts.append(_DIRECTIVE_STRICT_CRITIC)
    return "\n\n".join(parts)


def _run_parallel(tasks: list) -> list:
    """thunk 목록을 병렬 실행하고 입력 순서로 결과를 돌려준다(개별 실패는 None).

    ContextVar(자격증명·취소)를 워커에 넘기기 위해 컨텍스트 **사본**에서 실행한다
    (같은 Context 를 동시에 run 하면 "cannot enter context" 로 깨진다).
    max_workers 는 회의 1건 내 상한(CORE_MAX_PARALLEL) — 서버 전체 동시성은 상위 세마포어 담당.

    단, 인증 만료(AgentCLIAuthError)와 취소(OperationCancelled)는 **삼키지 않고 전파**한다.
    이 둘을 빈 결과로 묻으면 "재인증 필요"·"취소됨"을 상위가 알 수 없어 사용자에게 조용한
    빈 요약이 나간다(기존 service.enrich_to_contract 의 정책과 동일).
    """
    if not tasks:
        return []

    def _call(thunk):
        ctx = contextvars.copy_context()
        try:
            return ctx.run(thunk)
        except _PROPAGATE:
            raise
        except Exception:  # noqa: BLE001 — 한 창의 실패가 회의 전체를 죽이지 않는다
            traceback.print_exc()
            _count_failure("worker")   # 삼킨 실패를 세어 coreMeta 로 올린다(관측)
            return None

    if len(tasks) == 1:
        return [_call(tasks[0])]
    workers = max(1, min(config.CORE_MAX_PARALLEL, len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_call, tasks))


def _merge_action_payloads(payloads: list[dict]) -> list[dict]:
    """창별 추출 결과(dict) → 단일 목록. 완전 동일 텍스트만 결정적으로 병합.

    창 겹침 때문에 같은 과제가 두 번 나올 수 있다. 표현이 다른 중복은 여기서 손대지 않고
    critic 의 `drop`(중복 판정)에 맡긴다 — 텍스트 유사도로 임의 병합하면 다른 과제를 합칠 위험이
    있고, 그 손실은 되돌릴 수 없다.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for p in payloads:
        for item in p.get("action_items", []) or []:
            key = str(item.get("text") or "").strip()
            if not key:
                continue
            if key in merged:  # 동일 문장 → evidence 합집합
                ev = merged[key].get("evidence_seg_ids") or []
                merged[key]["evidence_seg_ids"] = list(
                    dict.fromkeys([*ev, *(item.get("evidence_seg_ids") or [])])
                )
                continue
            merged[key] = dict(item)
            order.append(key)
    out = []
    for i, key in enumerate(order):
        item = merged[key]
        item["id"] = i
        out.append(item)
    return out


def _concat_summaries(parts: list[MeetingSummary]) -> MeetingSummary:
    """reduce 실패 시 폴백: 부분 요약을 안건 번호만 재부여해 이어붙인다(결정적).

    LLM 병합이 안 되더라도 사용자는 요약을 받아야 한다. 중복 안건이 남을 수 있으나 유실은 없다.
    """
    out = MeetingSummary.empty()
    for p in parts:
        if p.meta and not out.meta.subject and getattr(p.meta, "subject", ""):
            out.meta.subject = p.meta.subject
        for blk in p.agenda:
            blk.no = len(out.agenda) + 1
            out.agenda.append(blk)
        for entry in p.agenda_index:
            entry.no = len(out.agenda_index) + 1
            out.agenda_index.append(entry)
    return out


def _items_for_critic(summary: MeetingSummary, actions: list[dict]) -> tuple[dict, dict, dict]:
    """critic 입력 JSON + (임시 id → 원본 객체) 매핑 2개.

    임시 id(S1.., A1..)를 쓰는 이유: 위치 인덱스나 본문 텍스트를 키로 쓰면 critic 이 조금만
    바꿔 답해도 대조가 깨진다. id 는 이 콜 안에서만 유효한 조인키다.
    """
    s_map: dict[str, object] = {}
    s_payload: list[dict] = []
    n = 0
    for blk in summary.agenda:
        for section in ("points", "decisions", "issues"):
            for it in getattr(blk, section):
                n += 1
                key = f"S{n}"
                s_map[key] = it
                s_payload.append(
                    {
                        "id": key,
                        "agenda": blk.title,
                        "section": section,
                        "text": it.text,
                        "evidence_seg_ids": list(it.evidence_seg_ids),
                    }
                )
    a_map: dict[str, dict] = {}
    a_payload: list[dict] = []
    for i, item in enumerate(actions, start=1):
        key = f"A{i}"
        a_map[key] = item
        a_payload.append(
            {
                "id": key,
                "text": item.get("text"),
                "owner": item.get("owner"),
                "due": item.get("due"),
                "evidence_seg_ids": list(item.get("evidence_seg_ids") or []),
            }
        )
    return {"summary_items": s_payload, "action_items": a_payload}, s_map, a_map


def _apply_critic(
    summary: MeetingSummary,
    actions: list[dict],
    critic: CriticResult,
    s_map: dict,
    a_map: dict,
) -> tuple[MeetingSummary, list[dict], dict]:
    """critic 판정을 결정적으로 적용. 반환: (요약, 액션, 적용 통계).

    - 요약: drop 만 반영(스키마에 flag 필드가 없어 flag 는 keep 으로 둔다).
    - 액션: drop 은 제거, flag 는 flag='확인필요'(기존 사람검토 큐 재사용).
    - 누락 보강(missing_actions)은 호출부에서 그라운딩 후 합친다.
    """
    stats = {"summaryDropped": 0, "actionsDropped": 0, "actionsFlagged": 0}
    drop_summary = {id(obj) for key, obj in s_map.items() if critic.summary_verdicts.get(key) == DROP}
    if drop_summary:
        for blk in summary.agenda:
            for section in ("points", "decisions", "issues"):
                kept = [it for it in getattr(blk, section) if id(it) not in drop_summary]
                stats["summaryDropped"] += len(getattr(blk, section)) - len(kept)
                setattr(blk, section, kept)

    # actions(원본 목록)을 기준으로 순회해 순서를 보존한다. a_map 에 없는 항목은 판정 대상이
    # 아니었으므로 그대로 통과시킨다(방어).
    key_by_obj = {id(v): k for k, v in a_map.items()}
    out_actions: list[dict] = []
    for item in actions:
        verdict = critic.action_verdicts.get(key_by_obj.get(id(item), ""))
        if verdict == DROP:
            stats["actionsDropped"] += 1
            continue
        if verdict == FLAG and not item.get("flag"):
            item["flag"] = FLAG_REVIEW
            stats["actionsFlagged"] += 1
        out_actions.append(item)
    return summary, out_actions, stats


def _korean_targets(summary: MeetingSummary | None, actions: list[dict]) -> list[dict]:
    """출력 언어 검사 대상 텍스트 필드 목록.

    각 항목은 {"id","text","kind","get","set"} 이며 set 으로 제자리 치환한다. 검사 범위는 사용자에게
    보이는 텍스트 전부(회의 주제·안건명·목차 한 줄·요약 항목·액션)다.
    """
    targets: list[dict] = []

    def add(kind: str, text: str, setter) -> None:
        if str(text or "").strip():
            targets.append({"id": f"L{len(targets) + 1}", "text": text, "kind": kind, "set": setter})

    if summary is not None:
        meta = summary.meta
        if meta is not None:
            add("subject", getattr(meta, "subject", ""), lambda v, m=meta: setattr(m, "subject", v))
        for entry in summary.agenda_index:
            add("index_title", entry.title, lambda v, e=entry: setattr(e, "title", v))
            add("index_summary", entry.summary, lambda v, e=entry: setattr(e, "summary", v))
        for blk in summary.agenda:
            add("agenda_title", blk.title, lambda v, b=blk: setattr(b, "title", v))
            for section in ("points", "decisions", "issues"):
                for it in getattr(blk, section):
                    add("summary_item", it.text, lambda v, o=it: setattr(o, "text", v))
    for item in actions:
        add("action", item.get("text", ""), lambda v, o=item: o.__setitem__("text", v))
    return targets


def _enforce_korean(
    summary: MeetingSummary | None,
    actions: list[dict],
    backend_name: str | None,
    *,
    localize: bool = True,
) -> tuple[MeetingSummary | None, list[dict], dict]:
    """출력 언어 보장(결정적 검사 → 수리 1콜 → 결정적 마감).

    1) 모든 산출 텍스트의 한글비를 코드가 재검사한다(language_gate.is_korean_output).
       전부 한국어면 **콜 없이 즉시 반환**한다 — 실측 958개 산출 텍스트가 전부 통과했으므로 평시 비용 0.
    2) 비한국어 항목만 모아 LocalizeStage 로 **1콜** 수리한다(번역만, 의미 보존).
    3) 수리 후에도 비한국어인 것은 결정적으로 마감한다:
         - 요약 항목  → 섹션에서 제거(빈 안건 블록·목차 동기화는 뒤이은 ground_summary 가 처리)
         - 액션       → flag='확인필요'(사람 검토 큐. 액션은 유실 비용이 커서 드롭하지 않는다)
         - 주제·안건명·목차 → 그대로 둔다(제목 하나 때문에 안건 전체를 버리는 건 과하다)
       이 비대칭은 저신뢰 근거 처리(§5)와 같은 원칙이다.
    """
    stats = {"nonKorean": 0, "repaired": 0, "droppedSummary": 0, "flaggedActions": 0, "calls": 0}
    targets = _korean_targets(summary, actions)
    bad = [t for t in targets if not language_gate.is_korean_output(t["text"])]
    stats["nonKorean"] = len(bad)
    if not bad:
        return summary, actions, stats

    if localize and backend_name and backend_name != "passthrough":
        stats["calls"] = 1
        payload = [{"id": t["id"], "kind": t["kind"], "text": t["text"]} for t in bad]
        try:
            fixed = LocalizeStage().run(payload, get_llm_backend(backend_name))
        except _PROPAGATE:
            raise
        except Exception:  # noqa: BLE001 — 수리 실패는 아래 결정적 마감으로 흡수
            traceback.print_exc()
            _count_failure("localize")
            fixed = {}
        for t in list(bad):
            new = fixed.get(t["id"])
            if new and language_gate.is_korean_output(new):
                t["set"](new)
                t["text"] = new
                stats["repaired"] += 1
                bad.remove(t)

    for t in bad:  # 수리 실패분 결정적 마감
        if t["kind"] == "summary_item" and summary is not None:
            for blk in summary.agenda:
                for section in ("points", "decisions", "issues"):
                    kept = [it for it in getattr(blk, section) if it.text != t["text"]]
                    if len(kept) != len(getattr(blk, section)):
                        stats["droppedSummary"] += len(getattr(blk, section)) - len(kept)
                        setattr(blk, section, kept)
        elif t["kind"] == "action":
            for item in actions:
                if item.get("text") == t["text"] and not item.get("flag"):
                    item["flag"] = FLAG_REVIEW
                    stats["flaggedActions"] += 1
    return summary, actions, stats


def _ground_actions(
    items: list[dict], segments: list[dict], low_conf: dict[int, str]
) -> list[dict]:
    """액션 그라운딩(결정적): 근거 멤버십 필터 → anchor 산출 → 저신뢰 단독근거 flag.

    요약과 달리 **드롭하지 않는다** — 액션은 유실 비용이 커서 사람 검토 큐로 보낸다
    (설계 docs/2026-07-30-영어환각-언어게이트-설계.md §5).
    """
    start_by_id = {int(s["id"]): float(s.get("start") or 0.0) for s in segments}
    out: list[dict] = []
    for item in items:
        ev = [int(x) for x in (item.get("evidence_seg_ids") or []) if int(x) in start_by_id]
        ev = list(dict.fromkeys(ev))
        item["evidence_seg_ids"] = ev
        if not ev:
            item["flag"] = item.get("flag") or FLAG_REVIEW  # 근거 0 = 환각 의심(기존 규약)
            item["anchor"] = None
        else:
            item["anchor"] = seconds_to_timestamp(min(start_by_id[s] for s in ev))
            if low_conf and all(s in low_conf for s in ev):
                item["flag"] = item.get("flag") or FLAG_REVIEW
        out.append(item)
    return out


def run_meeting_core(
    segments: list[dict],
    *,
    summarize_backend: str | None,
    extract_backend: str | None,
    critic_backend: str | None = None,
) -> dict:
    """회의 segment → {summary, actionItems, coreMeta}. 다중 agent core 본체.

    segments: [{id, start, end, text}] (정제본 text). 호출부(service)가 만들어 넘긴다.
    백엔드가 없거나 passthrough 면 그 단계를 건너뛴다(기존 폴백 정책 유지).
    critic_backend 미지정 시 요약 백엔드를 재사용한다.
    """
    # 이 실행에서 삼켜지는 실패를 모을 자리(관측). 워커 스레드에서도 같은 dict 가 보인다.
    failures: dict[str, int] = {}
    _FAILURES.set(failures)

    kept, low_conf, excluded = language_gate.partition(segments)
    profile = meeting_profile.profile_meeting(kept, low_conf, excluded)
    plan = meeting_profile.route(profile, kept)

    sum_on = bool(summarize_backend) and summarize_backend != "passthrough"
    ex_on = bool(extract_backend) and extract_backend != "passthrough"
    critic_name = critic_backend or summarize_backend
    critic_on = plan.critic and bool(critic_name) and critic_name != "passthrough"

    meta: dict = {
        "profile": profile.to_dict(),
        "plan": plan.to_dict(),
        "gate": {
            "excluded": {str(k): v for k, v in excluded.items()},
            "lowConf": {str(k): v for k, v in low_conf.items()},
        },
    }
    if not kept:
        meta["skipped"] = "no_segments"
        return {
            "summary": MeetingSummary.empty().to_dict(),
            "actionItems": [],
            "coreMeta": meta,
        }

    directives = _directives(plan)

    # ---------- Stage 2: 전문 agent 병렬 ----------
    tasks = []
    if sum_on:
        sum_backend = get_llm_backend(summarize_backend)
        for window in plan.windows:
            tasks.append(
                lambda w=window: SummarizeStage().run(
                    w,
                    sum_backend,
                    ctx={"extra_directives": directives, "low_conf_ids": low_conf},
                )
            )
    n_sum = len(tasks)
    if ex_on:
        ex_backend = get_llm_backend(extract_backend)
        for window in plan.windows:
            tasks.append(
                lambda w=window: ExtractStage().run(
                    w,
                    ex_backend,
                    ctx={"extra_directives": directives, "low_conf_ids": low_conf},
                )
            )
    results = _run_parallel(tasks)
    sum_parts = [r for r in results[:n_sum] if r is not None]
    ex_parts = [r for r in results[n_sum:] if r is not None]
    # `calls` 는 **계획된** 콜 수다(실행 전 태스크 수). 성공 수와 구분하지 않으면
    # "calls=3" 을 보고 "요약이 실행됐다"로 오독하게 된다 — 실제 사고 사례.
    meta["calls"] = {"summarize": n_sum, "extract": len(tasks) - n_sum, "reduce": 0, "critic": 0}
    meta["callsOk"] = {"summarize": len(sum_parts), "extract": len(ex_parts)}
    # JSON 파싱 실패는 예외가 아니라 빈 결과로 돌아온다 → 스테이지가 표시한 플래그를 여기서 센다.
    for part in sum_parts:
        if getattr(part, "parse_failed", False):
            _count_failure("summarizeParse")
    for part in ex_parts:
        if getattr(part, "parse_failed", False):
            _count_failure("extractParse")

    # ---------- Stage 3: 병합(reduce) ----------
    summary: MeetingSummary | None = None
    if sum_parts:
        if len(sum_parts) == 1:
            summary = sum_parts[0]
        else:
            reduced = None
            if sum_on:
                meta["calls"]["reduce"] = 1
                try:
                    reduced = ReduceStage().run(
                        [p.to_dict() for p in sum_parts],
                        get_llm_backend(summarize_backend),
                        ctx={"extra_directives": directives},
                    )
                except _PROPAGATE:
                    raise
                except Exception:  # noqa: BLE001 — 병합 실패는 결정적 폴백으로 흡수
                    traceback.print_exc()
                    _count_failure("reduce")
                    reduced = None
            if reduced is not None and reduced.agenda:
                summary = reduced
            else:
                meta["reduceFallback"] = True  # LLM 병합 실패 → 결정적 이어붙이기
                summary = _concat_summaries(sum_parts)
    actions = _merge_action_payloads([p.to_dict() for p in ex_parts]) if ex_parts else []

    # ---------- Stage 3': 검증(critic) 1패스 ----------
    critic_result = CriticResult.empty()
    if critic_on and (summary is not None or actions):
        items, s_map, a_map = _items_for_critic(summary or MeetingSummary.empty(), actions)
        if items["summary_items"] or items["action_items"]:
            meta["calls"]["critic"] = 1
            body = transcript_with_ids(kept, low_conf)
            try:
                critic_result = CriticStage().run(
                    body,
                    items,
                    get_llm_backend(critic_name),
                    ctx={"extra_directives": _directives(plan, for_critic=True)},
                )
            except _PROPAGATE:
                raise
            except Exception:  # noqa: BLE001 — 검증 실패가 산출을 죽이지 않는다
                traceback.print_exc()
                _count_failure("critic")
                critic_result = CriticResult.empty()
            if getattr(critic_result, "parse_failed", False):
                _count_failure("criticParse")
            if not critic_result.is_empty:
                summary_obj, actions, stats = _apply_critic(
                    summary or MeetingSummary.empty(), actions, critic_result, s_map, a_map
                )
                if summary is not None:
                    summary = summary_obj
                meta["critic"] = {**stats, "promptVersion": critic_result.prompt_version}
                if critic_result.missing_actions:
                    actions.extend(
                        {**m, "flag": None, "anchor": None} for m in critic_result.missing_actions
                    )
                    meta["critic"]["actionsAdded"] = len(critic_result.missing_actions)

    # ---------- Stage 4a: 출력 언어 보장(결정적 검사 → 필요 시 수리 1콜 → 드롭/flag) ----------
    summary, actions, lang_stats = _enforce_korean(
        summary,
        actions,
        summarize_backend if sum_on else extract_backend,
        localize=config.CORE_LOCALIZE_ENABLED,
    )
    meta["calls"]["localize"] = lang_stats.pop("calls", 0)
    meta["language"] = lang_stats

    # ---------- Stage 4b: 결정적 적용·그라운딩 ----------
    if summary is not None:
        summary = ground_summary(summary, kept, low_conf)
    actions = _ground_actions(actions, kept, low_conf)
    action_items = _action_items_from_payload({"action_items": actions})
    # summary 는 항상 dict 로 돌려준다(빈 구조체) — 호출부(웹 계약·재요약 미리보기)가 None 분기를
    # 따로 갖지 않게 한다. "요약이 비었다"는 신호는 coreMeta 로 판단한다.
    # 삼켜진 실패는 여기서만 드러난다 — 비어 있으면 필드를 싣지 않아 평시 로그를 깨끗이 둔다.
    if failures:
        meta["failures"] = dict(failures)
    return {
        "summary": summary.to_dict() if summary is not None else MeetingSummary.empty().to_dict(),
        "actionItems": action_items,
        "coreMeta": meta,
    }
