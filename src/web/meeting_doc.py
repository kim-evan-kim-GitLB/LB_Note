"""회의록 → HTML 렌더러(순수함수, 네트워크 없음).

Google Drive 동기화(google_drive.upsert_doc)가 이 HTML 을 소스로 files.create/update 하면
Google 이 Docs 네이티브 문서로 변환한다(target mimeType=application/vnd.google-apps.document).
변환 소스로 text/html 을 쓰는 이유: HTML→Docs 변환은 공식·장기 안정(제목/목록/굵게/표 매핑 신뢰).
text/markdown import 는 공식 미보증이라 제외한다(설계 결정, docs/2026-06-26...).

렌더 대상(meeting.data JSON, 프론트 types.ts 보존):
  - summary.agenda: [{no, title, decisions:[...], issues:[...]}] → h2/h3 + 목록
  - actionItems: [{text, owner, due, anchor, item_id}] → owner/due 표기 목록
  - transcript: [{segmentId, timestamp, speakerId, text, edited}] → 화자·시각 문단
  - participants: [...] → 머리말 목록

방어적 렌더: decisions/issues 항목이 문자열이든 {text:...} dict 든 모두 수용한다. 모든 사용자
데이터는 html.escape 로 이스케이프한다(Docs 변환 전 HTML 인젝션·깨짐 방지).

무거운 의존성 없음: stdlib(html) 만 사용.
"""
from __future__ import annotations

import html


def _esc(value: object) -> str:
    """None/숫자 포함 임의 값을 안전한 HTML 텍스트로. 개행은 그대로(문단 처리는 호출부)."""
    return html.escape(str(value if value is not None else ""), quote=False)


def _item_text(item: object) -> str:
    """decisions/issues/participants 항목 → 표시 텍스트. str 이거나 {text|name|title:...} dict 수용."""
    if isinstance(item, dict):
        for key in ("text", "name", "title", "label"):
            v = item.get(key)
            if v:
                return str(v)
        return ""
    return str(item or "")


def doc_title(meeting: dict) -> str:
    """드라이브 문서 제목. meeting.title 우선, 없으면 생성일 기반 폴백."""
    title = str(meeting.get("title") or "").strip()
    if title:
        return title
    created = str(meeting.get("createdAt") or "").strip()
    return f"회의록 {created}" if created else "회의록"


def _render_participants(meeting: dict) -> list[str]:
    parts = meeting.get("participants") or []
    names = [_esc(_item_text(p)) for p in parts if _item_text(p).strip()]
    if not names:
        return []
    return ["<p><strong>참석자:</strong> " + ", ".join(names) + "</p>"]


def _render_summary(meeting: dict) -> list[str]:
    agenda = ((meeting.get("summary") or {}).get("agenda")) or []
    if not agenda:
        return []
    out = ["<h2>요약</h2>"]
    for block in agenda:
        if not isinstance(block, dict):
            continue
        no = block.get("no")
        title = _item_text(block) or "안건"
        heading = f"{no}. {title}" if no is not None else title
        out.append(f"<h3>{_esc(heading)}</h3>")
        decisions = [d for d in (block.get("decisions") or []) if _item_text(d).strip()]
        if decisions:
            out.append("<p><strong>결정사항</strong></p><ul>")
            out.extend(f"<li>{_esc(_item_text(d))}</li>" for d in decisions)
            out.append("</ul>")
        issues = [i for i in (block.get("issues") or []) if _item_text(i).strip()]
        if issues:
            out.append("<p><strong>이슈</strong></p><ul>")
            out.extend(f"<li>{_esc(_item_text(i))}</li>" for i in issues)
            out.append("</ul>")
    return out


def _render_action_items(meeting: dict) -> list[str]:
    items = meeting.get("actionItems") or []
    rows = [it for it in items if isinstance(it, dict) and str(it.get("text") or "").strip()]
    if not rows:
        return []
    out = ["<h2>액션 아이템</h2>", "<ul>"]
    for it in rows:
        text = _esc(it.get("text"))
        meta: list[str] = []
        if str(it.get("owner") or "").strip():
            meta.append("담당: " + _esc(it.get("owner")))
        if str(it.get("due") or "").strip():
            meta.append("기한: " + _esc(it.get("due")))
        if str(it.get("anchor") or "").strip():
            meta.append("시각: " + _esc(it.get("anchor")))
        suffix = f" <em>({' · '.join(meta)})</em>" if meta else ""
        out.append(f"<li>{text}{suffix}</li>")
    out.append("</ul>")
    return out


def _render_transcript(meeting: dict, max_segments: int | None) -> list[str]:
    transcript = meeting.get("transcript") or []
    segs = [s for s in transcript if isinstance(s, dict) and str(s.get("text") or "").strip()]
    if not segs:
        return []
    out = ["<h2>전체 대화(transcript)</h2>"]
    truncated = False
    if max_segments is not None and len(segs) > max_segments:
        segs = segs[:max_segments]
        truncated = True
    for s in segs:
        ts = str(s.get("timestamp") or "").strip()
        speaker = str(s.get("speakerId") or "").strip()
        label_parts = [p for p in (ts, speaker) if p]
        label = f"[{' '.join(label_parts)}] " if label_parts else ""
        out.append(f"<p>{_esc(label)}{_esc(s.get('text'))}</p>")
    if truncated:
        out.append(
            "<p><em>(transcript 가 길어 일부만 표시했습니다. 전체 원문은 앱/원본 오디오에서 확인하세요.)</em></p>"
        )
    return out


def render_meeting_html(meeting: dict, *, max_transcript_segments: int | None = None) -> str:
    """회의록(요약+액션+transcript)을 단일 self-contained HTML 문서로 렌더.

    Google Docs 변환 소스로 쓴다. max_transcript_segments 지정 시 transcript 를 그 개수로 잘라
    Docs import 한도(~10MB)를 방어한다(원문은 DB/오디오에 보존되므로 손실 우려 낮음).
    """
    title = doc_title(meeting)
    body: list[str] = [f"<h1>{_esc(title)}</h1>"]
    created = str(meeting.get("createdAt") or "").strip()
    if created:
        body.append(f"<p><strong>일시:</strong> {_esc(created)}</p>")
    body.extend(_render_participants(meeting))
    body.extend(_render_summary(meeting))
    body.extend(_render_action_items(meeting))
    body.extend(_render_transcript(meeting, max_transcript_segments))
    inner = "\n".join(body)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko"><head><meta charset="utf-8">'
        f"<title>{_esc(title)}</title></head>\n"
        f"<body>\n{inner}\n</body></html>"
    )
