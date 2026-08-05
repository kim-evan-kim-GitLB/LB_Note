"""용어 사전 병합·검증·미리보기 (설계 docs/2026-08-05-회의록-품질-개선-설계.md 5순위).

층위: 파일 씨앗 → 전역(관리자) → 개인. 뒤가 앞을 덮는다.
  - `config/glossary.ko.json`: 저장소에 커밋된 씨앗. DB 가 비어 있으면 종전과 완전히 같게 동작한다.
  - `scope='global'`: 관리자가 관리하는 회사 공통 표기(인명·제품명).
  - `scope='user'`: 개인 항목. 같은 `source` 면 개인이 이긴다 — 남의 회의록을 바꾸지 않으면서
    자기 회의에서만 교정할 수 있어야 하기 때문.

**소급 적용하지 않는다.** 치환은 STT 적재 시점에 transcript 로 굳는다. 이미 저장된 회의록을
다시 훑지 않는 이유는 `evidence_seg_ids` 앵커 때문이다 — 본문만 바꾸면 요약·액션의 근거 참조가
어긋난다. UI 가 이 사실을 명시해야 한다(사용자가 사전을 등록하는 동기는 대개 "지난 회의록도
고쳐지나?" 이므로, 말하지 않으면 반드시 오해한다).
"""
from __future__ import annotations

import hashlib

from src.postprocess.glossary import (
    apply_corrections,
    find_matches,
    load_glossary,
    load_glossary_version,
    validate_glossary,
)
from src.web import auth

# 미리보기 비용 상한 — 회의가 수백 건인 사용자도 있으므로 최근 것부터 이만큼만 훑는다.
PREVIEW_MAX_MEETINGS = 30
PREVIEW_MAX_EXAMPLES = 5


def _file_seed() -> dict[str, str]:
    try:
        return load_glossary(None)
    except Exception:  # noqa: BLE001 — 씨앗 파일이 깨져도 DB 사전은 살아야 한다
        return {}


def layers_for(username: str | None) -> dict[str, list[dict]]:
    """층위별 항목 목록(UI 표시용). 개인 항목이 전역을 덮는지도 함께 표시한다."""
    seed = _file_seed()
    global_terms = auth.list_glossary_terms("global")
    personal = auth.list_glossary_terms("user", username) if username else []
    personal_keys = {t["source"] for t in personal}
    global_keys = {t["source"] for t in global_terms}
    for t in global_terms:
        t["overriddenByPersonal"] = t["source"] in personal_keys
    for t in personal:
        t["overridesGlobal"] = t["source"] in global_keys or t["source"] in seed
    return {
        "seed": [{"source": k, "target": v} for k, v in sorted(seed.items())],
        "global": global_terms,
        "personal": personal,
    }


def merged_for(username: str | None) -> dict[str, str]:
    """실제 치환에 쓰이는 최종 사전. 층위 순서대로 덮어쓴다."""
    merged = dict(_file_seed())
    for term in auth.list_glossary_terms("global"):
        merged[term["source"]] = term["target"]
    if username:
        for term in auth.list_glossary_terms("user", username):
            merged[term["source"]] = term["target"]
    return merged


def version_for(username: str | None) -> str:
    """재현성 스탬프. 파일 버전만으로는 "같은 입력 → 같은 출력"을 더 이상 보장하지 못한다
    (DB 항목이 얹히므로). 최종 사전 내용 자체를 해시해 스탬프에 섞는다."""
    merged = merged_for(username)
    if not merged:
        return load_glossary_version(None)
    digest = hashlib.sha256(
        "\n".join(f"{k}\t{v}" for k, v in sorted(merged.items())).encode("utf-8")
    ).hexdigest()[:8]
    return f"{load_glossary_version(None)}+db:{digest}"


def validate_candidate(
    username: str | None, scope: str, source: str, target: str
) -> list[str]:
    """저장 전 경고 목록(빈 목록 = 이상 없음).

    후보 항목을 **실제 병합 결과에 얹은 상태**로 검사한다. 항목 하나만 따로 보면
    "다른 항목과 겹친다" 같은 위험을 놓친다.
    """
    source, target = (source or "").strip(), (target or "").strip()
    if not source or not target:
        return ["찾을 말과 바꿀 말이 모두 필요합니다."]
    candidate = merged_for(username if scope == "user" else None)
    candidate[source] = target
    # 후보와 무관한 기존 항목의 경고까지 쏟아내면 사용자가 자기 입력 문제를 못 찾는다 →
    # 후보 표기가 언급된 경고만 남긴다.
    return [w for w in validate_glossary(candidate) if f"'{source}'" in w or f"'{target}'" in w]


def preview(source: str, target: str, meetings: list[dict]) -> dict:
    """후보 항목을 사용자의 **실제 회의록**에 돌려본 결과.

    샘플 문장이 아니라 본인 코퍼스여야 의미가 있다 — "이 항목이 내 회의록 몇 군데를 바꾸는가"가
    사용자가 실제로 알고 싶은 것이고, 오탐(엉뚱한 단어가 걸림)도 여기서만 드러난다.
    회의 목록은 호출부가 넘긴다(이 모듈이 DB 를 직접 열지 않게 해 테스트를 단순하게 둔다).
    """
    source, target = (source or "").strip(), (target or "").strip()
    rule = {source: target} if source and target else {}
    out: dict = {"matches": 0, "meetings": 0, "scanned": 0, "examples": []}
    if not rule:
        return out
    for meeting in (meetings or [])[:PREVIEW_MAX_MEETINGS]:
        out["scanned"] += 1
        hit_in_meeting = 0
        for entry in meeting.get("transcript") or []:
            text = str(entry.get("text") or "")
            hits = find_matches(text, rule)
            if not hits:
                continue
            hit_in_meeting += len(hits)
            if len(out["examples"]) < PREVIEW_MAX_EXAMPLES:
                out["examples"].append({
                    "meetingId": meeting.get("id"),
                    "title": meeting.get("title") or "",
                    "before": text,
                    "after": apply_corrections(text, rule)[0],
                })
        if hit_in_meeting:
            out["matches"] += hit_in_meeting
            out["meetings"] += 1
    return out
