"""저장된 회의(DB)의 transcript로 요약·추출만 재처리 — 새 프롬프트 반영.

STT 는 건너뛰고(이미 transcript 보유) summarize_meeting + extract_action_items 만 다시 돌린다.
방법2(요약→추출 힌트)도 그대로 태운다. 기본은 미리보기(출력만), --save 면 DB 갱신. 사용:
  sudo env PYTHONPATH=/app PATH=...claude.. .venv/bin/python tools/reprocess_meeting.py test1 --save
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import uuid

from src.web.service import (
    _summary_action_hints,
    extract_action_items,
    summarize_meeting,
)

DB = "output/web/meetings.db"
BACKEND = "agent_cli"


def _mmss(ts: str) -> float:
    p = [int(x) for x in str(ts).split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


def _to_frontend_items(items: list[dict], meeting_id: str, title: str) -> list[dict]:
    """백엔드 추출 형태 → 프론트 ActionItem(DB 저장 형태). ai.ts 매핑과 동일.

    due→dueDate, evidence_seg_ids→evidenceSegIds, id/status/confirmed 부여(AI 산출=확정 기본).
    """
    out = []
    for a in items:
        out.append({
            "id": uuid.uuid4().hex[:8],
            "text": a.get("text", ""),
            "status": "new",
            "meetingId": meeting_id,
            "meetingTitle": title,
            "dueDate": a.get("due") or None,
            "owner": a.get("owner"),
            "anchor": a.get("anchor"),
            "evidenceSegIds": a.get("evidence_seg_ids", []),
            "confirmed": True,
        })
    return out


def main(title: str, save: bool) -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT id, data FROM meetings WHERE title=?", (title,)).fetchone()
    if not row:
        print(f"회의 '{title}' 없음")
        return 1
    meeting_id = row["id"]
    m = json.loads(row["data"])
    transcript = m.get("transcript") or []
    starts = [_mmss(t.get("timestamp", "0:00")) for t in transcript]
    segs = []
    for i, t in enumerate(transcript):
        s = starts[i]
        e = starts[i + 1] if i + 1 < len(starts) else s + 5.0
        segs.append({"id": i, "start": s, "end": max(e, s), "cleaned": t.get("text", ""), "text": t.get("text", "")})

    print(f"[재처리] '{title}' id={meeting_id} segments={len(segs)} backend={BACKEND} save={save}", flush=True)

    summary = summarize_meeting(segs, backend_name=BACKEND)
    hints = _summary_action_hints(summary)
    print(f"\n[요약] agenda={len(summary.get('agenda') or [])}안건, 추출 힌트 {len(hints)}개")
    for h in hints:
        print("   힌트:", h)

    items = extract_action_items(segs, backend_name=BACKEND, summary_hints=hints)
    print(f"\n[액션아이템] {len(items)}건 (방법1 제거 + extract-1.3 능동도출 + 방법2 힌트)")
    for it in items:
        print(f"   - {it.get('text')} | owner={it.get('owner')} due={it.get('due')} anchor={it.get('anchor')}")

    if not save:
        print("\n[미리보기] --save 없음 → DB 미갱신.")
        return 0

    # DB 갱신: summary(구조체) + actionItems(프론트 형태)만 교체, 나머지 필드 보존.
    m["summary"] = summary
    m["actionItems"] = _to_frontend_items(items, meeting_id, title)
    m["updatedAt"] = dt.datetime.now().isoformat(timespec="seconds")
    c.execute(
        "UPDATE meetings SET data=?, updated_at=? WHERE id=?",
        (json.dumps(m, ensure_ascii=False), m["updatedAt"], meeting_id),
    )
    c.commit()
    print(f"\n[저장] DB 갱신 완료(id={meeting_id}). summary 1건 + actionItems {len(items)}건.")
    return 0


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a != "--save"]
    save = "--save" in sys.argv
    title = argv[0] if argv else "test2"
    raise SystemExit(main(title, save))
