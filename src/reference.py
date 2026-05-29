"""클로바 노트 export JSON → ko_office_answer 호환 Dialogs 스키마 변환.

클로바 노트의 export 스키마가 버전마다 다를 수 있어 best-effort 파싱:
- 최상위 dict 에 `segments` 또는 `result` 또는 `data.segments` 가 있는 경우
- segments[*] 필드: `text` / `content` / `transcript` 중 하나, `startTime` / `start` 가 ms 또는 sec
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _coerce_segments(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("segments", "Segments", "results", "result", "items"):
        if key in data and isinstance(data[key], list):
            return [s for s in data[key] if isinstance(s, dict)]
    if "data" in data and isinstance(data["data"], dict):
        return _coerce_segments(data["data"])
    return []


def _get_text(seg: dict) -> str:
    for key in ("text", "content", "transcript", "Speakertext", "speakerText"):
        if key in seg and isinstance(seg[key], str):
            return seg[key].strip()
    return ""


def _get_time(seg: dict, *keys: str) -> float:
    for key in keys:
        if key in seg and isinstance(seg[key], (int, float)):
            v = float(seg[key])
            return v / 1000.0 if v > 10_000 else v
    return 0.0


def _get_speaker(seg: dict) -> str | None:
    for key in ("speaker", "Speaker", "speakerLabel", "speakerName"):
        if key in seg:
            v = seg[key]
            if isinstance(v, dict):
                return v.get("name") or v.get("label") or str(v)
            return str(v)
    return None


def clova_to_ai_hub(clova_path: Path) -> dict:
    """클로바 노트 JSON 파일을 ko_office_answer.json 형식으로 변환.

    반환 스키마(ko_office_answer 호환):
    {"Dialogs": [{"DialogNum", "Speaker", "StartTime", "EndTime", "Speakertext", ...}]}
    """
    raw = json.loads(clova_path.read_text(encoding="utf-8"))
    segs = _coerce_segments(raw)

    dialogs = []
    for i, s in enumerate(segs, start=1):
        text = _get_text(s)
        if not text:
            continue
        start = _get_time(s, "startTime", "start", "StartTime", "begin")
        end = _get_time(s, "endTime", "end", "EndTime", "stop")
        dialogs.append({
            "DialogNum": i,
            "Speaker": _get_speaker(s) or f"S{i}",
            "StartTime": start,
            "EndTime": end if end > start else start,
            "SpeakTime": max(0.0, end - start),
            "Speakertext": text,
            "SentenceType": "Normal",
        })
    return {"DataSet": "ClovaNoteExport", "Dialogs": dialogs}
