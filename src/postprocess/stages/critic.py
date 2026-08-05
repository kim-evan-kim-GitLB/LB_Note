"""[V] 검증(critic) 스테이지 — 다중 agent core 의 독립 검증 1패스.

작성(요약·추출)과 검증을 다른 콜로 분리한다. 자기 산출물을 자기가 승인하면 환각이 그대로
통과하기 때문이다. critic 은 **판정만** 하고(본문 재작성 금지), 판정 적용은 호출부가 결정적으로
수행한다(요약 drop / 액션 drop·flag / 누락 액션 추가).

파싱은 방어적이다 — critic 출력이 깨지면 CriticResult.empty() 를 돌려 "판정 없음 = 전부 keep"
으로 흘린다. 검증 실패가 회의 산출을 죽이지 않게 하는 것이 이 파이프라인의 일관된 원칙이다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.postprocess.backends.base import LLMBackend
from src.postprocess.stages.clean import _load_prompt, _split_sections

# prompts/critic.ko.md (src/postprocess/stages/critic.py → ../../../prompts)
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "critic.ko.md"

_PROMPT_VERSION_RE = re.compile(r"prompt_version:\s*([^\s>]+)")

TRANSCRIPT_OPEN = "<<<TRANSCRIPT>>>"
TRANSCRIPT_CLOSE = "<<<END>>>"
ITEMS_OPEN = "<<<ITEMS>>>"
ITEMS_CLOSE = "<<<END_ITEMS>>>"

KEEP = "keep"
DROP = "drop"
FLAG = "flag"
_VERDICTS = frozenset({KEEP, DROP, FLAG})


def load_critic_prompt_version(path: Path | str | None = None) -> str:
    """critic.ko.md 헤더의 prompt_version 주석 반환(버전 스탬프)."""
    m = _PROMPT_VERSION_RE.search(_load_prompt(path or DEFAULT_PROMPT_PATH))
    return m.group(1) if m else "unknown"


@dataclass
class CriticResult:
    """critic 판정 결과. 키는 호출부가 부여한 임시 id(S1.., A1..)."""

    summary_verdicts: dict[str, str] = field(default_factory=dict)
    action_verdicts: dict[str, str] = field(default_factory=dict)
    missing_actions: list[dict] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    prompt_version: str = ""
    backend: str = ""
    # 관측 전용 — MeetingSummary.parse_failed 와 같은 목적.
    parse_failed: bool = False
    raw_head: str = ""

    @classmethod
    def empty(cls) -> "CriticResult":
        return cls()

    @property
    def is_empty(self) -> bool:
        return not (self.summary_verdicts or self.action_verdicts or self.missing_actions)

    def to_dict(self) -> dict:
        return {
            "summaryVerdicts": dict(self.summary_verdicts),
            "actionVerdicts": dict(self.action_verdicts),
            "missingActions": list(self.missing_actions),
            "promptVersion": self.prompt_version,
            "backend": self.backend,
        }


def _verdict_map(raw: object) -> tuple[dict[str, str], dict[str, str]]:
    """[{id, verdict, reason}] → ({id: verdict}, {id: reason}). 미지 verdict 는 버린다."""
    verdicts: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for entry in raw or []:  # type: ignore[union-attr]
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("id") or "").strip()
        val = str(entry.get("verdict") or "").strip().lower()
        if not key or val not in _VERDICTS:
            continue
        verdicts[key] = val
        reason = str(entry.get("reason") or "").strip()
        if reason:
            reasons[key] = reason
    return verdicts, reasons


def _missing_actions(raw: object) -> list[dict]:
    """누락 보강 후보 정규화. 근거(evidence_seg_ids)가 없는 항목은 버린다(환각 차단)."""
    out: list[dict] = []
    for entry in raw or []:  # type: ignore[union-attr]
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        ev: list[int] = []
        for x in entry.get("evidence_seg_ids") or []:
            try:
                ev.append(int(x))
            except (TypeError, ValueError):
                continue
        if not text or not ev:
            continue  # 근거 없는 보강은 채택하지 않는다
        out.append(
            {
                "text": text,
                "owner": entry.get("owner") or None,
                "owner_source": entry.get("owner_source") or None,
                "due": entry.get("due") or None,
                "evidence_seg_ids": ev,
            }
        )
    return out


def parse_critic_output(raw: str) -> CriticResult:
    """critic 출력 문자열 → CriticResult. 코드펜스·머리말 견고 처리(다른 스테이지와 동일)."""
    t = raw.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    if not t.startswith("{"):
        lo, hi = t.find("{"), t.rfind("}")
        if lo != -1 and hi != -1 and hi > lo:
            t = t[lo : hi + 1]
    data = json.loads(t)
    sv, sr = _verdict_map(data.get("summary_verdicts"))
    av, ar = _verdict_map(data.get("action_verdicts"))
    return CriticResult(
        summary_verdicts=sv,
        action_verdicts=av,
        missing_actions=_missing_actions(data.get("missing_actions")),
        reasons={**sr, **ar},
    )


def build_messages(
    transcript_body: str, items: dict, system_tmpl: str, user_tmpl: str
) -> list[dict]:
    """검증용 (system, user) 메시지. transcript·산출물을 각각 구분자로 격리(인젝션 방어)."""
    items_json = json.dumps(items, ensure_ascii=False, indent=1)
    user = user_tmpl.replace("{{TRANSCRIPT_WITH_IDS}}", transcript_body)
    user = user.replace("{{ITEMS_JSON}}", items_json)
    if TRANSCRIPT_OPEN not in user:
        user = f"{TRANSCRIPT_OPEN}\n{transcript_body}\n{TRANSCRIPT_CLOSE}\n{user}"
    if ITEMS_OPEN not in user:
        user = f"{user}\n{ITEMS_OPEN}\n{items_json}\n{ITEMS_CLOSE}"
    return [
        {"role": "system", "content": system_tmpl},
        {"role": "user", "content": user},
    ]


class CriticStage:
    """검증 스테이지(회의 단위 1콜). 판정만 하고 적용은 호출부."""

    name = "critic"

    def __init__(self, prompt_path: Path | str | None = None) -> None:
        self._prompt_path = prompt_path or DEFAULT_PROMPT_PATH

    def run(
        self,
        transcript_body: str,
        items: dict,
        backend: LLMBackend,
        ctx: dict | None = None,
    ) -> CriticResult:
        ctx = ctx or {}
        system_tmpl, user_tmpl = _split_sections(_load_prompt(self._prompt_path))
        if ctx.get("extra_directives"):
            system_tmpl = f"{system_tmpl}\n\n{ctx['extra_directives']}"
        messages = build_messages(transcript_body, items, system_tmpl, user_tmpl)
        raw = backend.generate(
            messages,
            schema=None,
            temperature=ctx.get("temperature", 0.0),
            max_tokens=ctx.get("max_tokens", 4096),
            seed=ctx.get("seed", 0),
        )
        try:
            result = parse_critic_output(raw)
        except (json.JSONDecodeError, ValueError):
            # 판정 파싱 실패 → 판정 없음(전부 keep). 검증 실패가 산출을 죽이지 않는다.
            # 다만 실백엔드에서의 실패는 조용히 넘기지 않는다(상위가 감사로그로 올린다).
            out = CriticResult.empty()
            out.parse_failed = backend.name != "passthrough"
            out.raw_head = (raw or "")[:200]
            return out
        result.prompt_version = load_critic_prompt_version(self._prompt_path)
        result.backend = backend.name
        return result
