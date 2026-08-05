"""용어 사전 치환 회귀 테스트 (설계 docs/2026-08-05-회의록-품질-개선-설계.md §3 5순위).

배경: `apply_corrections` 에 테스트가 하나도 없는 채로 사용자 편집 사전(CRUD UI)을 붙이려던
참이었다. 실측으로 확인된 결함 3종을 고치고 잠근다 — 사전은 전사 전체를 훑기 때문에
항목 하나가 잘못 걸리면 회의록 전체가 조용히 오염된다.

  1. ASCII 키가 한글 조사 앞에서 미적용: `\\b` 는 한글도 `\\w` 라 "Quan은" 에서 경계가 안 선다.
     한국어 회의록에서 영문 용어 뒤 조사는 기본형이라 사실상 절반이 안 먹던 셈.
  2. 짧은 한글 키의 부분어 오염: "회의"→"미팅" 이 "회의록"을 "미팅록"으로.
  3. 순차 치환에 의한 연쇄: 앞 규칙의 결과가 뒤 규칙에 다시 걸려 미팅→회의→미팅 왕복.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_glossary.py -q
"""
from __future__ import annotations

from src.postprocess.glossary import (
    apply_corrections,
    load_glossary,
    validate_glossary,
)


# ------------------------------------------------------------------ 버그 1: ASCII + 조사
def test_ascii키가_한글조사_앞에서도_치환된다():
    out, applied = apply_corrections("Quan은 좋고 Quan 도 좋다.", {"Quan": "Qwen"})
    assert out == "Qwen은 좋고 Qwen 도 좋다."
    assert applied == ["Qwen"]


def test_ascii키_부분일치는_여전히_막는다():
    """조사 대응을 넣느라 whole-word 보호를 잃으면 안 된다."""
    out, _ = apply_corrections("Quantum 은 무관하다. QuanX 도.", {"Quan": "Qwen"})
    assert out == "Quantum 은 무관하다. QuanX 도."


def test_ascii키_숫자경계():
    out, _ = apply_corrections("Quan2 와 Quan.", {"Quan": "Qwen"})
    assert out == "Quan2 와 Qwen."


# ------------------------------------------------------------------ 버그 2: 짧은 키 오염
def test_짧은키는_복합명사를_오염시키지_않는다():
    out, _ = apply_corrections("회의록을 국제회의 에서 공유", {"회의": "미팅"})
    assert out == "회의록을 국제회의 에서 공유"      # 록=복합명사, 국제=선행 한글


def test_짧은키도_조사가_붙으면_치환된다():
    out, _ = apply_corrections("회의를 회의에서 회의했다", {"회의": "미팅"})
    # 조사 자체는 건드리지 않는다("회의를"→"미팅를"). 받침에 따른 을/를·이/가 일치는
    # 사전의 책임이 아니라 정제(LLM) 단계의 일이다 — 여기서 손대면 결정성이 깨진다.
    assert out == "미팅를 미팅에서 미팅했다"


def test_단음절키_오염방지():
    """사전 주석이 경고하던 '환'→'환경'→'Qwen경' 사고를 코드가 막는다."""
    out, _ = apply_corrections("환경 설정과 환이 있다", {"환": "Qwen"})
    assert out == "환경 설정과 Qwen이 있다"


def test_긴_한글키는_접미가_붙어도_치환된다():
    """짧은 키 보호 규칙을 긴 키에 걸었더니 정상 교정 4건이 깨졌다(실전사 실측). 회귀 방지."""
    g = {"오퍼스": "Opus", "위스퍼": "Whisper"}
    out, _ = apply_corrections("오퍼스별 모델과 위스퍼라는 게", g)
    assert out == "Opus별 모델과 Whisper라는 게"


def test_긴_한글키는_붙여쓴_앞말도_통과():
    out, _ = apply_corrections("클로드오퍼스를 썼다", {"오퍼스": "Opus"})
    assert out == "클로드Opus를 썼다"


# ------------------------------------------------------------------ 버그 3: 연쇄 치환
def test_연쇄치환이_일어나지_않는다():
    """순차 적용이면 미팅→회의→미팅 으로 되돌아온다. 단일 패스라 한 번만 바뀐다."""
    out, applied = apply_corrections("미팅 자료", {"미팅": "회의", "회의": "미팅"})
    assert out == "회의 자료"
    assert applied == ["회의"]


def test_치환결과가_다른_규칙에_다시_걸리지_않는다():
    out, _ = apply_corrections("콴 모델", {"콴": "Qwen", "Qwen": "QWEN-2"})
    assert out == "Qwen 모델"


# ------------------------------------------------------------------ 기존 계약 유지
def test_긴_키가_짧은_키보다_먼저_적용된다():
    out, _ = apply_corrections("김원희 부장", {"김원": "X", "김원희": "김오네"})
    assert out == "김오네 부장"


def test_빈입력과_빈사전():
    assert apply_corrections("", {"가": "나"}) == ("", [])
    assert apply_corrections("본문", {}) == ("본문", [])


def test_적용목록은_중복없이_등장순서():
    out, applied = apply_corrections(
        "위스퍼와 채찌피티, 그리고 위스퍼", {"위스퍼": "Whisper", "채찌피티": "ChatGPT"}
    )
    assert out == "Whisper와 ChatGPT, 그리고 Whisper"
    assert applied == ["Whisper", "ChatGPT"]


def test_case_sensitive():
    out, _ = apply_corrections("QWEN 과 qwen", {"QWEN": "Qwen"})
    assert out == "Qwen 과 qwen"


def test_운영사전은_실제_전사에서_동작한다():
    """설계 의도대로 붙어 있는지 — 사전 자체가 깨지면 전부 무의미."""
    g = load_glossary()
    out, applied = apply_corrections("아까 위스퍼라는 게 소타라고 하는데요", g)
    assert "Whisper" in out and "SOTA" in out
    assert set(applied) == {"Whisper", "SOTA"}


# ------------------------------------------------------------------ 저장 전 검증
def test_검증이_단음절_한글키를_경고():
    w = validate_glossary({"환": "Qwen"})
    assert any("한 글자" in m for m in w)


def test_검증이_무의미항목과_빈값을_잡는다():
    assert any("같아" in m for m in validate_glossary({"회의": "회의"}))
    assert any("비어" in m for m in validate_glossary({"회의": "  "}))


def test_검증이_겹치는_항목을_알린다():
    assert any("겹칩니다" in m for m in validate_glossary({"김원": "X", "김원희": "김오네"}))


def test_운영사전_검증_경고는_알려진_1건뿐():
    """새 항목이 정책을 어기면 여기서 먼저 걸린다(현재 알려진 건: 단음절 '콴')."""
    w = validate_glossary(load_glossary())
    assert len(w) == 1 and "'콴'" in w[0]
