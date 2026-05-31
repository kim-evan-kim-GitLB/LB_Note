# Denoiser 향후 계획 — ZipEnhancer & GTCRN ONNX 스트리밍

> 이번 라운드에는 **구현하지 않음**. GTCRN(PyTorch, 오프라인 배치)만 통합됨.
> 사용자 의견이 오면 아래 절차로 추가한다.

## 1. ZipEnhancer (고품질 옵션, GPU)

- **모델**: ModelScope `iic/speech_zipenhancer_ans_multiloss_16k_base`
- **특성**: 16kHz native, Apache-2.0, ~2.04M params / ~62 GFLOPs, SOTA급 PESQ(DNS2020 3.69). **GPU 선호**(CPU 느림).
- **위치**: GTCRN 과 동일한 `AudioEnhancer` 인터페이스(`src/backends/enhancer_base.py`)로 `src/backends/zipenhancer_denoiser.py` 추가 → `src/stt.py:get_enhancer` 에 `"zipenhancer"` 분기 추가하면 끝(전처리 모듈/파이프라인 변경 불필요).

### 구현 시 주의
1. **의존성**: `modelscope` 스택 추가(무거움). `uv add modelscope` + 모델 다운로드(gitignore된 `models/zipenhancer/`).
2. **4GB VRAM 경합**: Cohere(GPU 상주)와 **동시 상주 불가**. `AudioEnhancer.load()/process()/unload()` 가 이미 Cohere `backend.load()` **이전**에 실행되므로(순차) 충돌 없음 — 단 ZipEnhancer 도 GPU를 쓰면 process 후 반드시 unload + `torch.cuda.empty_cache()`.
3. **연산량**: 62 GFLOPs라 83분 전체는 느림. → **VAD 무음압축을 먼저 적용해 발화 구간만** denoise (`ENHANCERS="...,zipenhancer"` 순서상 VAD가 enhancer 뒤라면, ZipEnhancer 용으론 VAD를 앞당기는 별도 순서 옵션 검토). GTCRN(CPU, 초경량)과 달리 전체 신호 처리는 비권장.
4. **평가**: GTCRN 대비 **다운스트림 WER A/B**(PESQ 아님)로 택일. baseline = 10분 슬라이스 rp1.2 WER 0.529.

## 2. GTCRN ONNX 스트리밍 (실시간/엣지)

- upstream `stream/` (gtcrn_stream.py) = frame-by-frame state 유지 스트리밍 버전 + ONNX export.
- 실시간/저지연이 필요할 때 `onnxruntime` 경로로 추가. 현재 오프라인 배치(PyTorch)로 충분하므로 보류.
- 추가 시: `onnxruntime` 의존성 + frame state 관리 래퍼. `AudioEnhancer` 인터페이스는 배치 입출력이므로 스트리밍은 별도 인터페이스 검토 필요.

## 우선순위
GTCRN(완료) → WER 측정 → 효과 부족 시 ZipEnhancer 도입 검토. 스트리밍은 제품화 단계에서.
