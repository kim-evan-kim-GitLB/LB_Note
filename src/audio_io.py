"""음성 파일을 16kHz mono float32 ndarray 로 정규화.

지원 형식: .wav (soundfile 직접) + .m4a / .mp3 / .aac / .amr (ffmpeg subprocess)
길이 한계: 180분 (10800초). 초과 시 ValueError.
출력: (np.ndarray shape=(N,), dtype=float32), sr=16000
"""
from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import numpy as np

TARGET_SR = 16000
MAX_DURATION_SEC = 180 * 60

SOUNDFILE_NATIVE = {".wav"}
FFMPEG_NEEDED = {".m4a", ".mp3", ".aac", ".amr"}
SUPPORTED_EXTS = SOUNDFILE_NATIVE | FFMPEG_NEEDED


def _to_mono(samples: np.ndarray) -> np.ndarray:
    if samples.ndim == 1:
        return samples
    if samples.ndim == 2:
        return samples.mean(axis=1).astype(np.float32)
    raise ValueError(f"예상치 못한 채널 shape: {samples.shape}")


def _resample(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return samples.astype(np.float32, copy=False)
    import librosa
    return librosa.resample(samples.astype(np.float32), orig_sr=src_sr, target_sr=dst_sr)


def _load_via_soundfile(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf
    samples, sr = sf.read(str(path), dtype="float32", always_2d=False)
    return _to_mono(samples), sr


def _load_via_ffmpeg(path: Path) -> tuple[np.ndarray, int]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            f"{path.suffix} 디코딩은 ffmpeg 가 필요합니다. "
            "설치: sudo apt-get install ffmpeg"
        )
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-i", str(path),
            "-ac", "1", "-ar", str(TARGET_SR),
            "-f", "wav", "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    import soundfile as sf
    samples, sr = sf.read(io.BytesIO(proc.stdout), dtype="float32", always_2d=False)
    return _to_mono(samples), sr


def load_audio(path: Path) -> tuple[np.ndarray, int]:
    """음성 파일을 16kHz mono float32 ndarray 로 로드.

    지원: .wav, .m4a, .mp3, .aac, .amr (그 외 ValueError)
    길이 한계: 180분 초과 시 ValueError
    반환: (samples, 16000)
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(
            f"지원하지 않는 형식: '{ext}'. "
            f"지원: {sorted(SUPPORTED_EXTS)}"
        )

    if ext in SOUNDFILE_NATIVE:
        samples, sr = _load_via_soundfile(path)
    else:
        samples, sr = _load_via_ffmpeg(path)

    samples = _resample(samples, sr, TARGET_SR)
    samples = samples.astype(np.float32, copy=False)

    duration = len(samples) / float(TARGET_SR)
    if duration > MAX_DURATION_SEC:
        raise ValueError(
            f"파일 길이 {duration:.1f}s 가 한계 {MAX_DURATION_SEC}s (180분) 를 초과합니다."
        )

    peak = float(np.abs(samples).max())
    if peak > 0.5:
        samples = (samples * (0.5 / peak)).astype(np.float32, copy=False)

    return samples, TARGET_SR


def duration_seconds(samples: np.ndarray, sr: int = TARGET_SR) -> float:
    return len(samples) / float(sr)
