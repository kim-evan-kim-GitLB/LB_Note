"""회의록 → HTML 렌더러(순수함수, 네트워크 없음).

Google Drive 동기화(google_drive.upsert_doc)가 이 HTML 을 소스로 files.create/update 하면
Google 이 Docs 네이티브 문서로 변환한다(target mimeType=application/vnd.google-apps.document).
변환 소스로 text/html 을 쓰는 이유: HTML→Docs 변환은 공식·장기 안정(제목/목록/굵게/표 매핑 신뢰).
text/markdown import 는 공식 미보증이라 제외한다(설계 결정, docs/2026-06-26...).

렌더 대상(meeting.data JSON, 프론트 types.ts 보존):
  - summary.agenda: [{no, title, time_range, points:[{text,anchor}], decisions:[...], issues:[...]}]
      → h3(안건 제목+시간범위) + points 목록(논의 본문) + 결정사항/이슈 목록
  - actionItems: [{text, owner, due, anchor, item_id}] → owner/due 표기 목록
  - transcript: [{segmentId, timestamp, speakerId, text, edited}] → 화자·시각 문단
  - participants: [...] → 머리말 목록

방어적 렌더: decisions/issues 항목이 문자열이든 {text:...} dict 든 모두 수용한다. 모든 사용자
데이터는 html.escape 로 이스케이프한다(Docs 변환 전 HTML 인젝션·깨짐 방지).

무거운 의존성 없음: stdlib(html) 만 사용.
"""
from __future__ import annotations

import datetime as dt
import html

# KST(고정 +9, DST 없음) — 문서/이메일 제목의 날짜·시간 스탬프용(app._KST 와 동일 규약).
_KST = dt.timezone(dt.timedelta(hours=9))


def _esc(value: object) -> str:
    """None/숫자 포함 임의 값을 안전한 HTML 텍스트로. 개행은 그대로(문단 처리는 호출부)."""
    return html.escape(str(value if value is not None else ""), quote=False)


def _title_stamp(iso: str) -> str:
    """ISO 타임스탬프(UTC 또는 naive) → KST `YYYY-MM-DD HH:MM`. 파싱 실패 시 빈 문자열."""
    try:
        d = dt.datetime.fromisoformat(iso.strip())
    except (ValueError, AttributeError):
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(_KST).strftime("%Y-%m-%d %H:%M")


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
    """문서/이메일 제목. `{meeting.title} (YYYY-MM-DD HH:MM)`(KST, createdAt 기준).

    제목 뒤에 생성 날짜·시간을 붙여 회의록을 시각으로 구분한다(앱/DB 의 title 자체는 불변 —
    이 스탬프는 표시용 파생값이라 재동기화해도 createdAt 기준으로 안정적, 중복 스탬프 없음).
    title 이 없으면 생성일 기반 폴백(스탬프 없이)한다.
    """
    title = str(meeting.get("title") or "").strip()
    if title:
        stamp = _title_stamp(str(meeting.get("createdAt") or ""))
        return f"{title} ({stamp})" if stamp else title
    created = str(meeting.get("createdAt") or "").strip()
    return f"회의록 {created}" if created else "회의록"


def _render_participants(meeting: dict) -> list[str]:
    parts = meeting.get("participants") or []
    names = [_esc(_item_text(p)) for p in parts if _item_text(p).strip()]
    if not names:
        return []
    return ["<p><strong>참석자:</strong> " + ", ".join(names) + "</p>"]


def _render_agenda_index(meeting: dict) -> list[str]:
    """안건 개요(agenda_index) — 번호·제목·한줄요약의 목차형 짤막 요약.

    시간대별 상세(agenda) 위에 얹어 '짤막한 요약 + 상세 요약' 두 층을 모두 문서에 담는다
    (앱 화면과 동일 구성 — 기존엔 상세만 렌더되고 이 목차가 누락됐었다).
    """
    index = ((meeting.get("summary") or {}).get("agenda_index")) or []
    rows: list[str] = []
    for e in index:
        if not isinstance(e, dict):
            continue
        title = str(e.get("title") or "").strip()
        if not title:
            continue
        no = e.get("no")
        head = f"{no}. {title}" if no is not None else title
        line = f"<strong>{_esc(head)}</strong>"
        summ = str(e.get("summary") or "").strip()
        if summ:
            line += f"<br>{_esc(summ)}"
        rows.append(f"<li>{line}</li>")
    if not rows:
        return []
    return ["<h3>안건 개요</h3>", "<ul>", *rows, "</ul>"]


def _render_summary(meeting: dict) -> list[str]:
    agenda = ((meeting.get("summary") or {}).get("agenda")) or []
    index_rows = _render_agenda_index(meeting)
    if not agenda and not index_rows:
        return []
    out = ["<h2>요약</h2>"]
    out.extend(index_rows)  # 목차형 짤막 요약(안건 개요) 먼저
    for block in agenda:
        if not isinstance(block, dict):
            continue
        no = block.get("no")
        title = _item_text(block) or "안건"
        heading = f"{no}. {title}" if no is not None else title
        time_range = str(block.get("time_range") or "").strip()
        if time_range:
            heading = f"{heading} ({time_range})"
        out.append(f"<h3>{_esc(heading)}</h3>")
        # 안건별 논의 내용(요약의 본문). points[].text 를 anchor 시각과 함께 목록으로 렌더한다.
        # 이 블록이 누락되면 안건 제목만 남고 실제 요약이 통째로 사라진다(핵심 수정 지점).
        points = [p for p in (block.get("points") or []) if _item_text(p).strip()]
        if points:
            out.append("<ul>")
            for p in points:
                text = _esc(_item_text(p))
                anchor = str(p.get("anchor") or "").strip() if isinstance(p, dict) else ""
                suffix = f" <em>({_esc(anchor)})</em>" if anchor else ""
                out.append(f"<li>{text}{suffix}</li>")
            out.append("</ul>")
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


def render_email_body(meeting: dict, note: str | None = None) -> str:
    """회의록 발송 이메일 본문(HTML) — 요약 + 액션아이템만(전사 제외). 전문은 첨부 PDF 로 보낸다.

    Gmail 본문으로 쓰는 self-contained HTML. 요약/액션이 모두 비면 안내 문구만 담는다.
    note: 발송자가 입력한 머리말(인사말) — 제목 바로 아래 삽입(이스케이프·줄바꿈 보존).
    """
    title = doc_title(meeting)
    body: list[str] = [f"<h2>{_esc(title)}</h2>"]
    if note:
        note_html = "<br>".join(_esc(ln) for ln in note.splitlines())
        body.append(f"<p>{note_html}</p>")
    created = str(meeting.get("createdAt") or "").strip()
    if created:
        body.append(f"<p><strong>일시:</strong> {_esc(_title_stamp(created) or created)}</p>")
    body.extend(_render_participants(meeting))
    summary = _render_summary(meeting)
    actions = _render_action_items(meeting)
    if not summary and not actions:
        body.append("<p>요약·액션아이템이 아직 없습니다. 상세 내용은 첨부된 회의록을 확인해 주세요.</p>")
    else:
        body.extend(summary)
        body.extend(actions)
    body.append("<hr><p style=\"color:#888;font-size:12px\">상세 회의록은 첨부 파일을 참고하세요.</p>")
    inner = "\n".join(body)
    return (
        '<!DOCTYPE html>\n<html lang="ko"><head><meta charset="utf-8">'
        f"<title>{_esc(title)}</title></head>\n"
        f'<body style="font-family:sans-serif;line-height:1.6">\n{inner}\n</body></html>'
    )


# ---------------------------------------------------------------------------
# 템플릿(구글 docs 양식) 치환용 평문 값 — google_docs.apply_template 이 사용.
# HTML 렌더와 별개로, 관리자 지정 템플릿의 {{key}} 플레이스홀더에 넣을 순수 텍스트를 만든다.
# 한 값 안의 개행(\n)은 Docs replaceAllText 시 새 문단이 된다(템플릿 문단 스타일 상속).
# ---------------------------------------------------------------------------
def _plain_summary(meeting: dict) -> str:
    lines: list[str] = []
    index = ((meeting.get("summary") or {}).get("agenda_index")) or []
    for e in index:
        if not isinstance(e, dict):
            continue
        title = str(e.get("title") or "").strip()
        if not title:
            continue
        no = e.get("no")
        head = f"{no}. {title}" if no is not None else title
        summ = str(e.get("summary") or "").strip()
        lines.append(f"{head} — {summ}" if summ else head)
    agenda = ((meeting.get("summary") or {}).get("agenda")) or []
    for block in agenda:
        if not isinstance(block, dict):
            continue
        no = block.get("no")
        title = _item_text(block) or "안건"
        heading = f"{no}. {title}" if no is not None else title
        time_range = str(block.get("time_range") or "").strip()
        if time_range:
            heading = f"{heading} ({time_range})"
        lines.append(heading)
        for p in (block.get("points") or []):
            t = _item_text(p).strip()
            if t:
                lines.append(f"  - {t}")
        decisions = [d for d in (block.get("decisions") or []) if _item_text(d).strip()]
        if decisions:
            lines.append("  [결정사항]")
            lines.extend(f"  - {_item_text(d).strip()}" for d in decisions)
        issues = [i for i in (block.get("issues") or []) if _item_text(i).strip()]
        if issues:
            lines.append("  [이슈]")
            lines.extend(f"  - {_item_text(i).strip()}" for i in issues)
    return "\n".join(lines)


def _plain_action_items(meeting: dict) -> str:
    items = meeting.get("actionItems") or []
    rows = [it for it in items if isinstance(it, dict) and str(it.get("text") or "").strip()]
    out: list[str] = []
    for it in rows:
        meta: list[str] = []
        if str(it.get("owner") or "").strip():
            meta.append("담당: " + str(it.get("owner")).strip())
        if str(it.get("due") or "").strip():
            meta.append("기한: " + str(it.get("due")).strip())
        if str(it.get("anchor") or "").strip():
            meta.append("시각: " + str(it.get("anchor")).strip())
        suffix = f" ({' · '.join(meta)})" if meta else ""
        out.append(f"- {str(it.get('text')).strip()}{suffix}")
    return "\n".join(out)


def _plain_transcript(meeting: dict) -> str:
    transcript = meeting.get("transcript") or []
    segs = [s for s in transcript if isinstance(s, dict) and str(s.get("text") or "").strip()]
    out: list[str] = []
    for s in segs:
        ts = str(s.get("timestamp") or "").strip()
        speaker = str(s.get("speakerId") or "").strip()
        label_parts = [p for p in (ts, speaker) if p]
        label = f"[{' '.join(label_parts)}] " if label_parts else ""
        out.append(f"{label}{str(s.get('text')).strip()}")
    return "\n".join(out)


def render_template_values(meeting: dict) -> dict[str, str]:
    """관리자 지정 Docs 템플릿의 플레이스홀더 → 실제 값(평문) 매핑.

    지원 플레이스홀더(템플릿에 `{{key}}` 형태로 넣으면 치환됨):
      title, date, attendees, department, author, summary, action_items, transcript
    값이 없으면 빈 문자열로 치환(잔여 `{{...}}` 텍스트가 문서에 남지 않도록).
    """
    parts = meeting.get("participants") or []
    names = [_item_text(p).strip() for p in parts if _item_text(p).strip()]
    meta = ((meeting.get("summary") or {}).get("meta")) or {}
    created = str(meeting.get("createdAt") or "").strip()
    return {
        "title": str(meeting.get("title") or "").strip() or doc_title(meeting),
        "date": _title_stamp(created) or created,
        "attendees": ", ".join(names),
        "department": str((meta or {}).get("department") or "").strip(),
        "author": str((meta or {}).get("author") or "").strip(),
        "summary": _plain_summary(meeting),
        "action_items": _plain_action_items(meeting),
        "transcript": _plain_transcript(meeting),
    }


def render_meeting_html(
    meeting: dict,
    *,
    max_transcript_segments: int | None = None,
    include_transcript: bool = True,
) -> str:
    """회의록을 단일 self-contained HTML 문서로 렌더.

    Google Docs 변환 소스로 쓴다. max_transcript_segments 지정 시 transcript 를 그 개수로 잘라
    Docs import 한도(~10MB)를 방어한다(원문은 DB/오디오에 보존되므로 손실 우려 낮음).

    include_transcript=False 면 '전체 대화(transcript)' 섹션을 통째로 생략하고 요약+액션 중심으로
    렌더한다(Drive 저장 문서 정책 — 전체 대화 로그는 앱/DB 에 보존되므로 문서엔 요약만 담는다).
    """
    title = doc_title(meeting)
    body: list[str] = [f"<h1>{_esc(title)}</h1>"]
    created = str(meeting.get("createdAt") or "").strip()
    if created:
        body.append(f"<p><strong>일시:</strong> {_esc(_title_stamp(created) or created)}</p>")
    body.extend(_render_participants(meeting))
    body.extend(_render_summary(meeting))
    body.extend(_render_action_items(meeting))
    if include_transcript:
        body.extend(_render_transcript(meeting, max_transcript_segments))
    inner = "\n".join(body)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko"><head><meta charset="utf-8">'
        f"<title>{_esc(title)}</title></head>\n"
        f"<body>\n{inner}\n</body></html>"
    )
