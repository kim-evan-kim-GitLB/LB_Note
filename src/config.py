from __future__ import annotations

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
# ENHANCERS: 쉼표 구분 순서. ""=none, 예: "wpe,gtcrn" (dereverb→denoise).
# 기본 "wpe": 표준 파이프라인 = WPE(울림 제거)→VAD→모델. 울림 제거 단독은 대역제한 음원에서도
# WER 개선·반복환각 억제 검증됨(asr test.m4a 55분: WER 0.39→0.36, CER 0.25→0.22, P2 환각 2→0).
# GTCRN(denoise)은 대역제한에 net-negative라 기본 제외(필요 시 ENHANCERS=wpe,gtcrn). 끄려면 ENHANCERS="".
ENHANCERS = os.getenv("ENHANCERS", "wpe")
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

# --- P5 증거기반 향상 라우팅 (opt-in, 기본 OFF) ---
# "1"=on. 켜면 enhancers 명시 안 했을 때만 품질 측정→decide_enhancers 로 자동 선택.
AUTO_ENHANCE = os.getenv("AUTO_ENHANCE", "") not in ("", "0", "false", "False")
AUTO_ENHANCE_SNR_LO = float(os.getenv("AUTO_ENHANCE_SNR_LO", "12.0"))
AUTO_ENHANCE_CUTOFF_OK_HZ = float(os.getenv("AUTO_ENHANCE_CUTOFF_OK_HZ", "7000.0"))


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
    }


def assert_cuda_or_raise() -> str:
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA 미가용 — WSL2 GPU passthrough 확인 필요")
    return torch.cuda.get_device_name(0)
