"""target_sec 설정 노출 + STT 신호 관통 회귀 테스트
(설계 docs/2026-08-05-회의록-품질-개선-설계.md §3 2·3순위).

검증 불변식:
  - target_sec 상한은 **모델 config 에서 읽는다**(상수 하드코딩 금지 — 모델 교체 시 침묵 회귀).
    실제 조건은 `duration <= max_audio_clip_s - overlap_chunk_second` 라 경계값 자체는 안전하다.
  - 환경변수는 그 상한으로 클램프되고, CLI 로 넘긴 초과값은 거부된다(회의 하나가 통째로 죽기 전에).
  - STT 가 붙인 판정 신호(redecoded)가 정규화·정제·계약 단계에서 **버려지지 않는다**.
    예전에는 normalize_segments 가 {id,start,end,text} 만 남겨 후단에 도달하지 못했다.

실행: sudo PYTHONPATH=/app .venv/bin/python -m pytest tests/test_target_sec_and_flag_passthrough.py -q
"""
from __future__ import annotations

import json

import pytest

from src import config
from src.postprocess.schema import CleanedSegment, normalize_segments
from src.postprocess.validate import FLAG_REVIEW, repair_or_degrade


# --------------------------------------------------------------- target_sec 상한
def test_상한은_모델_config에서_읽는다():
    """35(max_audio_clip_s)가 아니라 30(=35-5)이 실제 임계다."""
    cfg = json.loads((config.COHERE_MODEL_PATH / "config.json").read_text(encoding="utf-8"))
    expected = float(cfg["max_audio_clip_s"]) - float(cfg["overlap_chunk_second"])
    assert config.STT_TARGET_SEC_MAX == expected


def test_경계값_자체는_안전하다():
    """모델 비교가 `<=` 라 임계값과 같은 값은 재분할되지 않는다 — 기본값(30.0)을 거부하면 안 된다."""
    assert config.STT_TARGET_SEC <= config.STT_TARGET_SEC_MAX
    assert config.STT_TARGET_SEC == 30.0


def test_모델_config_없으면_기본값_유지():
    """모델 미설치 환경에서 import 가 깨지면 안 된다."""
    from pathlib import Path
    orig = config.COHERE_MODEL_PATH
    try:
        config.COHERE_MODEL_PATH = Path("/nonexistent/model")
        assert config._model_resegment_threshold_sec() == 30.0
    finally:
        config.COHERE_MODEL_PATH = orig


def test_초과값은_거부된다(tmp_path):
    """조용히 진행하면 이후 모든 세그먼트의 타임스탬프가 어긋난다."""
    from src.pipeline import run_pipeline
    with pytest.raises(ValueError, match="재분할 임계"):
        run_pipeline(audio_path=tmp_path / "x.wav", out_dir=tmp_path, target_sec=31.0)


# --------------------------------------------------------------- 신호 관통
def test_normalize_segments가_redecoded를_보존():
    out = normalize_segments([
        {"start": 0.0, "end": 5.0, "text": "가", "redecoded": True},
        {"start": 5.0, "end": 9.0, "text": "나"},
    ])
    assert out[0]["redecoded"] is True
    assert "redecoded" not in out[1]      # falsy 는 싣지 않는다(계약을 넓히지 않음)


def test_normalize_segments가_알려지지_않은_키는_버린다():
    """화이트리스트 방식 — 계약이 조용히 넓어지지 않아야 한다."""
    out = normalize_segments([{"start": 0.0, "end": 1.0, "text": "가", "내부용": "값"}])
    assert "내부용" not in out[0]


def test_degrade해도_신호를_잃지_않는다():
    """정제가 실패해 원문으로 되돌려도 '이 구간은 재디코딩됐다'는 사실은 남아야 한다."""
    seg = CleanedSegment(
        id=0, start=0.0, end=5.0, original="원문", cleaned="", redecoded=True,
    )
    out = repair_or_degrade(seg, require_edit=True)
    assert out.flag == FLAG_REVIEW        # degrade 경로를 탔다
    assert out.redecoded is True


def test_core_segments가_신호를_전달():
    from src.web.service import core_segments
    out = core_segments([
        {"id": 0, "start": 0.0, "end": 5.0, "cleaned": "가", "text": "가", "redecoded": True},
        {"id": 1, "start": 5.0, "end": 9.0, "cleaned": "나", "text": "나"},
    ])
    assert out[0]["redecoded"] is True
    assert "redecoded" not in out[1]
