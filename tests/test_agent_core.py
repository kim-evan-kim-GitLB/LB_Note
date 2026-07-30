"""다중 agent core 회귀 테스트 — 라우터 + 병렬 전문 agent + critic 1패스.

설계: docs/2026-07-30-영어환각-언어게이트-설계.md §2 (2026-07-30 결정).

검증 불변식:
  - 프로파일링·라우팅은 **결정적**이다(LLM 미사용). case 4종 판정과 창 분할이 입력만으로 결정된다.
  - 장시간 회의는 창으로 나뉘고, 각 창이 **병렬로** 호출된다(요약·추출 동시).
  - critic 판정이 결정적으로 적용된다: 요약 drop / 액션 drop·flag / 누락 보강(근거 있는 것만).
  - 저신뢰 단독근거 항목은 요약은 드롭, 액션은 flag='확인필요'(비대칭 — 유실 비용 차이).
  - 인증 만료(AgentCLIAuthError)는 병렬 워커에서도 **삼켜지지 않고 전파**된다.
  - 어느 단계가 실패해도 회의 산출이 죽지 않는다(빈 결과 degrade / 결정적 폴백).

가짜 백엔드로 LLM 없이 검증한다(콜 수·병렬성·프롬프트 주입 내용까지 관찰).

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_agent_core.py -q
"""
from __future__ import annotations

import contextlib
import json
import threading

from src import config
from src.postprocess import meeting_profile as mp
from src.postprocess import orchestrator as orch
from src.postprocess.backends.agent_cli import AgentCLIAuthError
from src.postprocess.backends.base import LLMBackend, LLMCapabilities

KO = "그래서 이번 배포 일정을 다음 주로 확정하고 준비 작업을 진행하기로 했습니다"


def _segs(n: int, *, start: float = 0.0, step: float = 20.0, text: str = KO) -> list[dict]:
    return [
        {"id": i, "start": start + i * step, "end": start + (i + 1) * step, "text": f"{text} {i}"}
        for i in range(n)
    ]


@contextlib.contextmanager
def _cfg(**overrides):
    old = {k: getattr(config, k) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(config, k, v)


class _FakeBackend(LLMBackend):
    """호출을 기록하고 역할별로 고정 JSON 을 돌려주는 가짜 백엔드.

    system 프롬프트의 지문으로 역할(요약/추출/병합/검증)을 판별한다 — orchestrator 가 어느
    프롬프트를 어떤 순서로 몇 번 호출했는지 관찰하기 위함.
    """

    name = "fake"

    def __init__(self, *, summary=None, actions=None, critic=None, reduce=None, delay=0.0):
        self._summary = summary or {"meta": {}, "agenda_index": [], "agenda": []}
        self._actions = actions or {"action_items": []}
        self._critic = critic
        self._reduce = reduce
        self._delay = delay
        self.calls: list[dict] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(json_mode=True, ctx_window=200000, determinism="reproducible")

    def generate(self, messages, schema=None, temperature=0.0, max_tokens=4096, seed=0) -> str:
        system = messages[0]["content"]
        user = messages[1]["content"]
        # 프롬프트별 **고유** 문구로 판별한다. "검증"·"판정" 같은 일반 단어는 추출 프롬프트에도
        # 나오므로(완료판정 등) 역할이 뒤바뀐다 — 실제로 그 함정에 한 번 빠졌다.
        if "부분 요약" in system:
            role = "reduce"
        elif "근거와 대조해" in system:
            role = "critic"
        elif "실행 과제" in system:
            role = "extract"
        else:
            role = "summarize"
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self._delay:
                threading.Event().wait(self._delay)
            with self._lock:
                self.calls.append({"role": role, "system": system, "user": user})
        finally:
            with self._lock:
                self.concurrent -= 1
        if role == "reduce":
            return json.dumps(self._reduce or self._summary, ensure_ascii=False)
        if role == "critic":
            return json.dumps(self._critic or {}, ensure_ascii=False)
        if role == "extract":
            return json.dumps(self._actions, ensure_ascii=False)
        return json.dumps(self._summary, ensure_ascii=False)

    def roles(self) -> list[str]:
        return [c["role"] for c in self.calls]


@contextlib.contextmanager
def _patched(backend):
    """orchestrator 가 쓰는 백엔드 팩토리를 가짜로 교체."""
    orig = orch.get_llm_backend
    orch.get_llm_backend = lambda name=None: backend  # type: ignore[assignment]
    try:
        yield
    finally:
        orch.get_llm_backend = orig  # type: ignore[assignment]


def _summary_fixture(seg_ids=(0, 1)) -> dict:
    return {
        "meta": {"subject": "배포 회의"},
        "agenda_index": [{"no": 1, "title": "배포 일정", "summary": "다음 주 확정"}],
        "agenda": [
            {
                "no": 1,
                "title": "배포 일정",
                "points": [{"text": "다음 주 배포로 확정", "evidence_seg_ids": list(seg_ids)}],
                "decisions": [],
                "issues": [],
            }
        ],
    }


# ---------- 프로파일링·라우팅(결정적) ----------
def test_profile_and_route_short_meeting() -> None:
    segs = _segs(5)
    prof = mp.profile_meeting(segs, {}, {})
    assert prof.n_segments == 5 and prof.cases == []
    plan = mp.route(prof, segs)
    assert len(plan.windows) == 1 and not plan.conservative and not plan.strict_owner
    assert plan.critic is True


def test_route_long_form_splits_windows() -> None:
    with _cfg(CORE_WINDOW_SEGMENTS=10, CORE_WINDOW_OVERLAP=2, CORE_MULTI_TOPIC_SEGMENTS=1000):
        segs = _segs(25)
        prof = mp.profile_meeting(segs, {}, {})
        assert mp.CASE_LONG_FORM in prof.cases
        plan = mp.route(prof, segs)
        assert plan.is_map_reduce and len(plan.windows) >= 3
        # 창은 겹친다(경계 논의 유실 방지)
        assert plan.windows[0][-1]["id"] > plan.windows[1][0]["id"] - 10


def test_cases_hallucination_multi_topic_low_quality() -> None:
    with _cfg(CORE_MULTI_TOPIC_SEGMENTS=5, CORE_LOW_QUALITY_RATIO=0.15, CORE_WINDOW_SEGMENTS=1000):
        segs = _segs(10)
        prof = mp.profile_meeting(segs, {2: "mostly_non_korean", 3: "mostly_non_korean"}, {9: "non_korean"})
        assert mp.CASE_HALLUCINATION in prof.cases
        assert mp.CASE_MULTI_TOPIC in prof.cases
        assert mp.CASE_LOW_QUALITY in prof.cases, prof.to_dict()
        plan = mp.route(prof, segs)
        assert plan.conservative and plan.strict_owner and plan.strict_critic


def test_duplicate_ratio_marks_low_quality() -> None:
    segs = [{"id": i, "start": i, "end": i + 1, "text": "같은 말 반복"} for i in range(5)]
    prof = mp.profile_meeting(segs, {}, {})
    assert prof.duplicate_ratio >= 0.3 and mp.CASE_LOW_QUALITY in prof.cases


def test_split_windows_no_split_when_small() -> None:
    segs = _segs(5)
    assert mp.split_windows(segs, 10, 2) == [segs]
    assert mp.split_windows([], 10, 2) == []


# ---------- 병렬 실행 ----------
def test_windows_run_in_parallel() -> None:
    """창별 요약·추출이 동시에 돈다(순차면 max_concurrent==1)."""
    backend = _FakeBackend(summary=_summary_fixture(), delay=0.05)
    with _cfg(CORE_WINDOW_SEGMENTS=5, CORE_WINDOW_OVERLAP=1, CORE_MAX_PARALLEL=4,
              CORE_CRITIC_ENABLED=False, CORE_MULTI_TOPIC_SEGMENTS=1000), _patched(backend):
        out = orch.run_meeting_core(_segs(20), summarize_backend="fake", extract_backend="fake")
    assert backend.max_concurrent > 1, f"병렬 실행 안 됨: {backend.max_concurrent}"
    assert out["coreMeta"]["calls"]["summarize"] > 1
    assert out["coreMeta"]["calls"]["extract"] > 1


def test_auth_error_propagates_from_parallel_worker() -> None:
    """인증 만료는 병렬 워커에서도 전파돼야 한다(빈 요약으로 묻으면 재인증 안내가 불가)."""

    class _AuthFail(_FakeBackend):
        def generate(self, messages, schema=None, temperature=0.0, max_tokens=4096, seed=0) -> str:
            raise AgentCLIAuthError("claude 인증 만료")

    backend = _AuthFail()
    with _cfg(CORE_WINDOW_SEGMENTS=5, CORE_WINDOW_OVERLAP=1), _patched(backend):
        try:
            orch.run_meeting_core(_segs(12), summarize_backend="fake", extract_backend="fake")
        except AgentCLIAuthError:
            return
    raise AssertionError("AgentCLIAuthError 가 전파되지 않았다")


def test_stage_failure_degrades_without_killing_meeting() -> None:
    """한 스테이지가 깨져도 회의 산출은 계속된다(요약 실패 → 액션만이라도)."""

    class _SummaryBroken(_FakeBackend):
        def generate(self, messages, schema=None, temperature=0.0, max_tokens=4096, seed=0) -> str:
            if "액션아이템(실행 과제)" in messages[0]["content"]:
                return json.dumps({"action_items": [{"text": "배포 준비", "evidence_seg_ids": [0]}]})
            raise RuntimeError("요약 백엔드 폭발")

    with _cfg(CORE_CRITIC_ENABLED=False), _patched(_SummaryBroken()):
        out = orch.run_meeting_core(_segs(4), summarize_backend="fake", extract_backend="fake")
    assert out["summary"]["agenda"] == []           # 빈 요약으로 degrade
    assert len(out["actionItems"]) == 1             # 액션은 살아남음


# ---------- 언어 게이트 연동 ----------
def test_excluded_segment_not_injected_and_reported() -> None:
    backend = _FakeBackend(summary=_summary_fixture())
    segs = _segs(3)
    segs.append(
        {
            "id": 3,
            "start": 60.0,
            "end": 88.0,
            "text": "Okay, that's it. I have some room to be around the two girls and they were there.",
        }
    )
    with _cfg(CORE_CRITIC_ENABLED=False), _patched(backend):
        out = orch.run_meeting_core(segs, summarize_backend="fake", extract_backend="fake")
    injected = backend.calls[0]["user"]
    assert "[3]" not in injected, "제외 세그먼트가 프롬프트에 들어갔다"
    assert out["coreMeta"]["gate"]["excluded"] == {"3": "non_korean"}


def test_low_conf_marked_in_prompt_and_summary_dropped() -> None:
    """저신뢰 단독근거: 프롬프트에 `~` 표시 + 요약 항목은 결정적으로 드롭."""
    segs = _segs(3)
    segs.append({"id": 3, "start": 60.0, "end": 68.0, "text": "OK, LGTM"})  # 짧은 라틴 → low_conf
    backend = _FakeBackend(summary=_summary_fixture(seg_ids=(3,)))  # 저신뢰만 근거로 삼은 요약
    with _cfg(CORE_CRITIC_ENABLED=False), _patched(backend):
        out = orch.run_meeting_core(segs, summarize_backend="fake", extract_backend="fake")
    assert "[3]~" in backend.calls[0]["user"], "저신뢰 표시(~) 누락"
    assert out["coreMeta"]["gate"]["lowConf"] == {"3": "mostly_non_korean"}
    assert out["summary"]["agenda"] == [], "저신뢰 단독근거 요약이 살아남았다"


def test_low_conf_action_is_flagged_not_dropped() -> None:
    """액션은 유실 비용이 커서 드롭이 아니라 flag='확인필요'."""
    segs = _segs(3)
    segs.append({"id": 3, "start": 60.0, "end": 68.0, "text": "OK, LGTM"})
    backend = _FakeBackend(
        actions={"action_items": [{"text": "확인 후 회신", "evidence_seg_ids": [3]}]}
    )
    with _cfg(CORE_CRITIC_ENABLED=False), _patched(backend):
        out = orch.run_meeting_core(segs, summarize_backend="fake", extract_backend="fake")
    assert len(out["actionItems"]) == 1
    assert out["actionItems"][0]["flag"] == "확인필요"


# ---------- critic 적용 ----------
def test_critic_drops_flags_and_adds() -> None:
    critic = {
        "summary_verdicts": [{"id": "S1", "verdict": "drop", "reason": "근거 없음"}],
        "action_verdicts": [
            {"id": "A1", "verdict": "flag", "reason": "확정 애매"},
            {"id": "A2", "verdict": "drop", "reason": "중복"},
        ],
        "missing_actions": [
            {"text": "일정 공유", "evidence_seg_ids": [1]},
            {"text": "근거없는 보강", "evidence_seg_ids": []},  # 근거 0 → 채택 안 됨
        ],
    }
    backend = _FakeBackend(
        summary=_summary_fixture(seg_ids=(0, 1)),
        actions={
            "action_items": [
                {"text": "배포 준비", "evidence_seg_ids": [0]},
                {"text": "배포 준비 중복", "evidence_seg_ids": [1]},
            ]
        },
        critic=critic,
    )
    with _cfg(CORE_CRITIC_ENABLED=True), _patched(backend):
        out = orch.run_meeting_core(_segs(4), summarize_backend="fake", extract_backend="fake")
    assert "critic" in backend.roles(), backend.roles()
    assert out["summary"]["agenda"] == [], "critic drop 이 요약에 반영되지 않았다"
    texts = [i["text"] for i in out["actionItems"]]
    assert "배포 준비" in texts and "배포 준비 중복" not in texts
    assert "일정 공유" in texts and "근거없는 보강" not in texts
    flagged = [i for i in out["actionItems"] if i["text"] == "배포 준비"][0]
    assert flagged["flag"] == "확인필요"
    stats = out["coreMeta"]["critic"]
    assert stats["actionsDropped"] == 1 and stats["actionsFlagged"] == 1 and stats["actionsAdded"] == 1


def test_critic_broken_output_is_noop() -> None:
    """critic 출력이 깨지면 판정 없음(전부 keep) — 검증 실패가 산출을 죽이지 않는다."""

    class _BadCritic(_FakeBackend):
        def generate(self, messages, schema=None, temperature=0.0, max_tokens=4096, seed=0) -> str:
            out = super().generate(messages, schema, temperature, max_tokens, seed)
            return "이건 JSON 이 아닙니다" if self.calls[-1]["role"] == "critic" else out

    backend = _BadCritic(
        summary=_summary_fixture(),
        actions={"action_items": [{"text": "배포 준비", "evidence_seg_ids": [0]}]},
    )
    with _cfg(CORE_CRITIC_ENABLED=True), _patched(backend):
        out = orch.run_meeting_core(_segs(4), summarize_backend="fake", extract_backend="fake")
    assert len(out["summary"]["agenda"]) == 1
    assert [i["text"] for i in out["actionItems"]] == ["배포 준비"]


def test_critic_can_be_disabled() -> None:
    backend = _FakeBackend(summary=_summary_fixture())
    with _cfg(CORE_CRITIC_ENABLED=False), _patched(backend):
        out = orch.run_meeting_core(_segs(4), summarize_backend="fake", extract_backend="fake")
    assert "critic" not in backend.roles()
    assert out["coreMeta"]["calls"]["critic"] == 0


# ---------- 병합(reduce) ----------
def test_reduce_called_for_long_form_and_fallback_on_failure() -> None:
    merged = _summary_fixture(seg_ids=(0, 5))
    backend = _FakeBackend(summary=_summary_fixture(), reduce=merged)
    with _cfg(CORE_WINDOW_SEGMENTS=5, CORE_WINDOW_OVERLAP=1, CORE_CRITIC_ENABLED=False,
              CORE_MULTI_TOPIC_SEGMENTS=1000), _patched(backend):
        out = orch.run_meeting_core(_segs(15), summarize_backend="fake", extract_backend="fake")
    assert "reduce" in backend.roles()
    assert out["coreMeta"]["calls"]["reduce"] == 1
    assert len(out["summary"]["agenda"]) == 1, "병합 결과가 쓰이지 않았다"

    class _ReduceBroken(_FakeBackend):
        def generate(self, messages, schema=None, temperature=0.0, max_tokens=4096, seed=0) -> str:
            if "부분 요약들을 하나로 병합" in messages[0]["content"]:
                return "깨진 출력"
            return super().generate(messages, schema, temperature, max_tokens, seed)

    b2 = _ReduceBroken(summary=_summary_fixture())
    with _cfg(CORE_WINDOW_SEGMENTS=5, CORE_WINDOW_OVERLAP=1, CORE_CRITIC_ENABLED=False,
              CORE_MULTI_TOPIC_SEGMENTS=1000), _patched(b2):
        out2 = orch.run_meeting_core(_segs(15), summarize_backend="fake", extract_backend="fake")
    assert out2["coreMeta"]["reduceFallback"] is True
    assert len(out2["summary"]["agenda"]) >= 1, "폴백에서도 요약은 남아야 한다"


def test_duplicate_actions_merged_by_identical_text() -> None:
    """창 겹침으로 같은 문장이 두 번 나오면 evidence 합집합으로 병합(표현이 다르면 critic 이 판단)."""
    backend = _FakeBackend(
        summary=_summary_fixture(),
        actions={"action_items": [{"text": "배포 준비", "evidence_seg_ids": [0]}]},
    )
    with _cfg(CORE_WINDOW_SEGMENTS=5, CORE_WINDOW_OVERLAP=1, CORE_CRITIC_ENABLED=False,
              CORE_MULTI_TOPIC_SEGMENTS=1000), _patched(backend):
        out = orch.run_meeting_core(_segs(15), summarize_backend="fake", extract_backend="fake")
    assert len([i for i in out["actionItems"] if i["text"] == "배포 준비"]) == 1


# ---------- case 별 지시문 주입 ----------
def test_case_directives_injected_into_system_prompt() -> None:
    backend = _FakeBackend(summary=_summary_fixture())
    with _cfg(CORE_MULTI_TOPIC_SEGMENTS=2, CORE_CRITIC_ENABLED=False,
              CORE_WINDOW_SEGMENTS=1000), _patched(backend):
        orch.run_meeting_core(_segs(4), summarize_backend="fake", extract_backend="fake")
    assert any("다주제" in c["system"] for c in backend.calls), "strict_owner 지시문 누락"


def test_passthrough_backends_skip_llm_entirely() -> None:
    backend = _FakeBackend()
    with _patched(backend):
        out = orch.run_meeting_core(
            _segs(4), summarize_backend="passthrough", extract_backend="passthrough"
        )
    assert backend.calls == []
    assert out["summary"]["agenda"] == [] and out["actionItems"] == []


def test_empty_segments_short_circuits() -> None:
    out = orch.run_meeting_core([], summarize_backend="fake", extract_backend="fake")
    assert out["coreMeta"]["skipped"] == "no_segments"
    assert out["actionItems"] == []


def _run() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"PASS test_agent_core ({len(fns)} cases)")


if __name__ == "__main__":
    _run()
