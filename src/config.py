from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

# Cohere 메인 승격(2026-05-27): 모델은 본 프로젝트 안 models/ 에서 자체 호스팅.
# samples 는 아직 archive(lb-note-archive/samples) 와 공유 중 — 후속 정리 시 이전 예정.
_ARCHIVE_PROJECT = Path("/home/evan/Claude_workspace/lb-note-archive")
SAMPLES_DIR = Path(os.getenv("SAMPLES_DIR", str(_ARCHIVE_PROJECT / "samples")))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(PROJECT_ROOT / "output")))

STT_BACKEND = os.getenv("STT_BACKEND", "cohere")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "Korean")

# VAD 분할 청크 배치 디코딩 크기. 8 -> 32 상향(2026-07-28 실측 근거):
#   - 속도    51.5분 음원 18.1s -> 8.2s (2.22배). GPU 를 다른 작업과 공유하는 상태에서 측정
#   - 품질    WER 0.400 -> 0.398 (bs=1 기준선 0.397). 차이는 노이즈 수준
#   - VRAM    reserved peak 5,834 -> 7,344 MiB (+1.5GB)
#   - 발자국  VRAM x 점유시간 103 -> 58 GB.s (0.57배). 짧게 끝나 남을 오히려 덜 방해한다
#   - 취소    배치 경계 취소 지연 상한 0.58s -> 1.08s (수용 범위)
# 배치가 클수록 커널 실행 횟수가 줄어 시분할 경합에 강하다(bs=8 은 경합 시 RTFx 40% 손실,
# bs=32 는 거의 무손실). 단 VRAM 이 빠듯한 환경에서는 낮춰야 한다 — 8 은 4GB VRAM 시절 값.
STT_BATCH_SIZE = int(os.getenv("STT_BATCH_SIZE", "32"))

# 이 프로세스가 쓸 수 있는 VRAM 상한(MiB). 0 이면 제한 없음.
# 이 서버의 GPU 는 STT 전용이 아니다 — 다른 작업자의 학습/실험과 공유한다. 상한이 없으면 STT 가
# 예산을 넘어 남의 작업을 OOM 으로 죽일 수 있고, 그쪽은 수 시간치 학습을 잃는다. 상한을 걸면
# STT 가 자기 몫 안에서 먼저 실패하므로 사고가 우리 쪽에서 멈춘다.
# 기본 20GB — 실측 최대 사용량(슬롯 2 x bs=32 동시 = 13.2GB)보다 충분히 크고, 정상 동작을
# 제약하지 않는다. GPU 를 독점하는 환경이면 0 으로 꺼도 된다.
STT_VRAM_CAP_MB = int(os.getenv("STT_VRAM_CAP_MB", "20480"))

COHERE_MODEL_PATH = Path(
    os.getenv(
        "COHERE_MODEL_PATH",
        str(PROJECT_ROOT / "models" / "cohere-transcribe-03-2026"),
    )
)
COHERE_DTYPE = os.getenv("COHERE_DTYPE", "bfloat16")
COHERE_QUANTIZATION = os.getenv("COHERE_QUANTIZATION", "")

HF_TOKEN = os.getenv("HF_TOKEN") or None

# --- 프론트엔드 전처리 ---
# ENHANCERS: 쉼표 구분 순서. ""=none(기본), 예: "wpe" / "wpe,gtcrn" (dereverb→denoise).
#
# 2026-07-29 기본값을 "wpe" -> "" 로 변경. 이유는 비용/효과가 맞지 않아서다.
#   WPE 는 nara_wpe 단일채널 구현이고 **CPU 전용**이라(src/backends/wpe_dereverb.py) 처리
#   시간이 오디오 길이에 정비례한다. 실측(tools/bench_wpe_ablation.py, 30분 음원):
#     ENHANCERS=wpe  -> STT 170.3s (실효 RTFx 10.6)
#     ENHANCERS=""   -> STT   6.1s (실효 RTFx 295.1)   = 28배
#   즉 STT 시간의 96% 가 WPE 였고, 문서상 RTFx 232 는 전처리를 뺀 수치였다.
#   정확도 대가는 작았다(tools/bench_wpe_wer.py, ax 회의 83분 / Clova reference):
#     wpe  WER 0.3934 CER 0.2539  (STT 456.3s)
#     none WER 0.3994 CER 0.2598  (STT  15.4s)   = 30배 빠르고 WER +0.006
#
# 단, 효과는 음원 의존적이다 — 잔향이 심한 원거리 녹음에서는 WPE 가 값을 할 수 있다.
# 울림이 심한 음원은 개별적으로 ENHANCERS=wpe 로 처리하고, 근본적으로는 auto-enhance
# 라우팅(src/pipeline.py 의 chosen 경로)으로 "잰 뒤 필요할 때만" 태우는 것이 목표다(후속).
#
# GTCRN(denoise)은 대역제한에 net-negative라 기본 제외(필요 시 ENHANCERS=wpe,gtcrn).
ENHANCERS = os.getenv("ENHANCERS", "")
# VAD_BACKEND: ""=off, "silero"
VAD_BACKEND = os.getenv("VAD_BACKEND", "")
VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.5"))
VAD_MIN_SPEECH_SEC = float(os.getenv("VAD_MIN_SPEECH_SEC", "0.2"))
VAD_MIN_SILENCE_SEC = float(os.getenv("VAD_MIN_SILENCE_SEC", "0.3"))
VAD_PAD_SEC = float(os.getenv("VAD_PAD_SEC", "0.25"))
VAD_MAX_SILENCE_SEC = float(os.getenv("VAD_MAX_SILENCE_SEC", "0.5"))
GTCRN_MODEL_PATH = Path(
    os.getenv(
        "GTCRN_MODEL_PATH",
        str(PROJECT_ROOT / "models" / "gtcrn" / "model_trained_on_dns3.tar"),
    )
)

# --- P2 반복-환각 collapse (결정적 후처리 백스톱, 기본 ON) ---
# "1"=on. 디코딩 분포 불변, 임계 미만 no-op라 켜두는 게 안전(정상 발화 보존).
REPETITION_GUARD = os.getenv("REPETITION_GUARD", "1") not in ("", "0", "false", "False")
# 연속 반복을 이 횟수까지 허용("하 하 하"); 초과분만 접음.
REPETITION_MAX_REPEAT = int(os.getenv("REPETITION_MAX_REPEAT", "3"))
# 폭주 길이 제한: VAD 청크 길이(초)×이 값 = max_new_tokens 상한(폭주를 짧게 자름).
REPETITION_TOKENS_PER_SEC = int(os.getenv("REPETITION_TOKENS_PER_SEC", "16"))
REPETITION_MNT_FLOOR = int(os.getenv("REPETITION_MNT_FLOOR", "256"))
REPETITION_MNT_CEIL = int(os.getenv("REPETITION_MNT_CEIL", "1024"))

# --- 적응형 재디코딩 (영어 음차 드리프트 구제, 기본 ON) ---
# 근거: docs/2026-08-04-영어전사-드리프트-진단.md
# 마이크에서 멀고 무음이 섞인 구간을 30초 청크로 한 번에 디코딩하면 모델이 한국어로 인식하지
# 못하고 발음을 영어 철자로 옮긴다("출장"→"chojang"). 같은 오디오를 8초로 잘라 다시 디코딩하면
# 실제 내용이 복원된다(실측: 드리프트 8건 한글비 0.037 → 0.799).
# 전역으로 target_sec 을 낮추는 방식은 채택하지 않았다 — 정상 음원에서 WER 이 나빠진다
# (ax 음원 실측 0.398 → 0.420). 그래서 **실패한 청크만** 골라 다시 디코딩한다.
STT_REDECODE = os.getenv("STT_REDECODE", "1") not in ("", "0", "false", "False")
# 재디코딩 트리거: 청크 텍스트의 한글비율이 이 값 미만이면 후보.
# 기준은 **언어 게이트의 저신뢰 임계**(LANG_GATE_LOW_CONF_RATIO, 아래에 정의)와 같게 둔다.
# 즉 "게이트가 저신뢰 이하로 떨어뜨릴 청크를 떨어뜨리기 전에 한 번 더 살려본다"가 규칙이다.
# 처음에는 exclude 임계(0.15)에 맞췄으나 그 값은 "한글이 사실상 0"인 청크만 잡아서, 같은 방식으로
# 깨져 있던 회색지대(mostly_non_korean, 한글비 0.16~0.37)를 놓쳤다 — 문제 음원 실측에서 0.15 는
# 저신뢰 21→16 에 그쳤고 0.45 는 21→1 로 줄였다(한글비 0.617→0.895, 게이트 제외 12→0,
# 추가 15건 전부 개선·악화 0). 정상 한국어 세그먼트의 최저 한글비는 0.59(게이트 캘리브레이션)라
# 아래로 마진이 있고, ax(정상 음원)는 두 값 모두 후보 1건 — WER 0.397 / CER 0.251 회귀 없음.
STT_REDECODE_RATIO = float(os.getenv("STT_REDECODE_RATIO", "0.45"))
# 너무 짧은 텍스트는 지표가 불안정하다(한두 단어 영어는 진짜 발화일 확률이 높다) → 후보 제외.
STT_REDECODE_MIN_CHARS = int(os.getenv("STT_REDECODE_MIN_CHARS", "40"))
# 재디코딩 시 하위 청크 목표 길이(초). 8s 실측이 가장 좋았다(한글비 0.799 vs 15s 0.601).
STT_REDECODE_TARGET_SEC = float(os.getenv("STT_REDECODE_TARGET_SEC", "8.0"))
# 안전판: 전체 청크의 이 비율을 넘게 후보가 잡히면 재디코딩을 건너뛴다. 음원 전체가 비한국어
# (실제 영어 회의 등)인 경우 전부 재디코딩해봐야 의미가 없고 시간만 배로 든다.
STT_REDECODE_MAX_FRACTION = float(os.getenv("STT_REDECODE_MAX_FRACTION", "0.5"))


def _model_resegment_threshold_sec(default: float = 30.0) -> float:
    """모델이 입력을 내부 재분할하기 시작하는 길이(초).

    특징추출기는 `duration <= max_audio_clip_s - overlap_chunk_second` 일 때만 재분할하지 않는다
    (등호 포함이므로 임계값 자체는 안전). 이 값을 넘는 청크를 주면 출력 행 수가 입력 청크 수보다
    많아지고, 배치 경로가 조용히 어긋난다 — 그래서 상수로 박지 않고 **모델 config 에서 읽는다**.
    모델을 교체했는데 상수가 남아 있으면 같은 사고가 조용히 재발한다.
    """
    try:
        cfg = json.loads((COHERE_MODEL_PATH / "config.json").read_text(encoding="utf-8"))
        v = float(cfg["max_audio_clip_s"]) - float(cfg["overlap_chunk_second"])
        return v if v > 0 else default
    except Exception:  # noqa: BLE001 — 모델 미설치·필드 부재 시 기존 기본값 유지
        return default


# VAD 분할 청크의 목표 최대 길이(초). 배포에서 조정 가능해야 한다 — 음질이 나쁜 음원은 짧은 청크가
# 유리하지만(진단 문서 §3), **정상 음원에서는 WER 이 나빠지므로 기본값을 낮추지 말 것**
# (ax 실측 30초 0.398 -> 15초 0.420). 개별 음원 재처리에만 쓴다.
# 상한은 모델 재분할 임계로 클램프한다 — 넘겨도 런타임에 걸리지만, 그때는 회의 하나가 통째로 죽는다.
STT_TARGET_SEC_MAX = _model_resegment_threshold_sec()
STT_TARGET_SEC = min(float(os.getenv("STT_TARGET_SEC", "30.0")), STT_TARGET_SEC_MAX)

# --- P5 증거기반 향상 라우팅 (opt-in, 기본 OFF) ---
# "1"=on. 켜면 enhancers 명시 안 했을 때만 품질 측정→decide_enhancers 로 자동 선택.
AUTO_ENHANCE = os.getenv("AUTO_ENHANCE", "") not in ("", "0", "false", "False")
AUTO_ENHANCE_SNR_LO = float(os.getenv("AUTO_ENHANCE_SNR_LO", "12.0"))
AUTO_ENHANCE_CUTOFF_OK_HZ = float(os.getenv("AUTO_ENHANCE_CUTOFF_OK_HZ", "7000.0"))


# --- 언어 게이트 (영어 환각 전사 방어, 기본 ON) ---
# 설계·캘리브레이션: docs/2026-07-30-영어환각-언어게이트-설계.md §1-2·§3.
# 실측 마진(환각 한글비율 0.00~0.01 / 정상 최저 0.59)이 커서 기본값으로 안전하다.
# "0" 으로 두면 게이트 전체 no-op(안전 스위치) — 요약·추출 입력이 종전과 동일해진다.
LANG_GATE_ENABLED = os.getenv("LANG_GATE_ENABLED", "1") not in ("", "0", "false", "False")
# 제외 임계: 한글비율이 이 값 미만 **그리고** 한글 문자수가 MIN_HANGUL 미만이면 주입 제외.
LANG_GATE_EXCLUDE_RATIO = float(os.getenv("LANG_GATE_EXCLUDE_RATIO", "0.15"))
LANG_GATE_MIN_HANGUL = int(os.getenv("LANG_GATE_MIN_HANGUL", "5"))
# 제외 최소 길이(문자): 이보다 짧은 라틴 조각은 버리지 않고 저신뢰 표시만 한다.
# 실측 환각은 365~427자였고 짧은 조각은 진짜 발화("OK", "LGTM 네")일 확률이 높다.
LANG_GATE_MIN_CHARS = int(os.getenv("LANG_GATE_MIN_CHARS", "40"))
# 문자 폭주 제외: cps 가 이 값 초과 그리고 한글비율이 CPS_RATIO 미만일 때만(빠른 한국어 보호).
LANG_GATE_MAX_CPS = float(os.getenv("LANG_GATE_MAX_CPS", "12.0"))
LANG_GATE_CPS_RATIO = float(os.getenv("LANG_GATE_CPS_RATIO", "0.5"))
# 저신뢰(회색지대) 임계: 한글비율이 이 값 미만이면 표시만 한다(정상 발화 실측 최저 0.59).
LANG_GATE_LOW_CONF_RATIO = float(os.getenv("LANG_GATE_LOW_CONF_RATIO", "0.45"))
# 산출물 출력 언어 보장 임계(입력 판정과 별개): 요약·액션 텍스트의 한글비가 이 값 미만이면
# 비한국어 산출로 보고 수리(1콜)→실패 시 요약 드롭·액션 flag. 실측 정상 최저 0.308 이라 0.20 은
# 오탐 0 에 여유가 있다(958개 산출 텍스트 캘리브레이션, docs/2026-07-30 §6-2).
LANG_OUT_MIN_RATIO = float(os.getenv("LANG_OUT_MIN_RATIO", "0.20"))
# 비한국어 산출 수리 콜 사용 여부. 끄면 수리 없이 곧바로 드롭/flag 로 처리한다.
CORE_LOCALIZE_ENABLED = os.getenv("CORE_LOCALIZE_ENABLED", "1") not in ("", "0", "false", "False")


# --- 다중 agent core (라우터 + 병렬 전문 agent + critic 1패스, 2026-07-30 결정) ---
# 설계: docs/2026-07-30-영어환각-언어게이트-설계.md §2. 케이스 판정은 결정적(LLM 미사용)이며
# LLM 콜은 전부 병렬로 돌린다 — 시간 예산이 "단일 콜의 2배 이내"라서 순차 실행은 불가.
CORE_ENABLED = os.getenv("CORE_ENABLED", "1") not in ("", "0", "false", "False")
# critic(검증 1패스). 끄면 요약·액션을 그대로 통과시킨다(콜 1개 절약).
CORE_CRITIC_ENABLED = os.getenv("CORE_CRITIC_ENABLED", "1") not in ("", "0", "false", "False")
# 동시 LLM 콜 상한. 회의 1건 안에서만 적용(서버 전체 동시성은 상위 세마포어가 통제).
CORE_MAX_PARALLEL = int(os.getenv("CORE_MAX_PARALLEL", "4"))
# 장시간 회의 분할(map) 창 크기·겹침(세그먼트 개수 단위). 겹침은 창 경계 논의 유실 방지용.
CORE_WINDOW_SEGMENTS = int(os.getenv("CORE_WINDOW_SEGMENTS", "120"))
CORE_WINDOW_OVERLAP = int(os.getenv("CORE_WINDOW_OVERLAP", "3"))
# long_form 판정: 이 길이(초)를 넘으면 분할. 기본 40분(단일 콜 맥락·시간 한계 경험치).
CORE_LONG_FORM_SEC = float(os.getenv("CORE_LONG_FORM_SEC", "2400"))
# multi_topic 프록시: 세그먼트가 이 수 이상이면 다주제로 보고 owner 추론을 억제한다.
CORE_MULTI_TOPIC_SEGMENTS = int(os.getenv("CORE_MULTI_TOPIC_SEGMENTS", "80"))
# low_quality 판정 임계: 저신뢰 비율 / 동일 본문 반복 비율.
CORE_LOW_QUALITY_RATIO = float(os.getenv("CORE_LOW_QUALITY_RATIO", "0.15"))
CORE_DUPLICATE_RATIO = float(os.getenv("CORE_DUPLICATE_RATIO", "0.3"))


def parse_enhancers(spec: str | None) -> list[str]:
    """ENHANCERS 스펙 문자열 → 정규화된 이름 리스트."""
    if not spec:
        return []
    return [x.strip().lower() for x in spec.split(",") if x.strip()]


def env_status() -> dict:
    return {
        "stt_backend": STT_BACKEND,
        "stt_language": STT_LANGUAGE,
        "hf_token_set": HF_TOKEN is not None,
        "samples_dir_exists": SAMPLES_DIR.exists(),
        "cohere_model_exists": COHERE_MODEL_PATH.exists(),
        "enhancers": parse_enhancers(ENHANCERS),
        "vad_backend": VAD_BACKEND or None,
        "gtcrn_model_exists": GTCRN_MODEL_PATH.exists(),
        "lang_gate": LANG_GATE_ENABLED,
    }


def assert_cuda_or_raise() -> str:
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA 미가용 — WSL2 GPU passthrough 확인 필요")
    return torch.cuda.get_device_name(0)
