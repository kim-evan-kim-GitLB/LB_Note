# Docker 배포 — 모델 bake 자기완결 이미지

> 결정: **모델을 이미지에 굽는다(A안).** 서버에선 모델 셋업 없이 `docker run` 만.
> 두 변종: `cu121`(160=RTX 4090) / `cu128`(171=RTX PRO 6000 Blackwell).

## 그림

```
노트북(약함): 코드 작성 → git push           # 가벼움, 빌드 안 함
   160(4090): git pull → hf download(모델) → docker build → 171로 이미지 전송
      171(Blackwell): docker run 만           # 인터넷/모델 셋업 불필요 ✅
```

이미지 안에 **코드 + 파이썬환경 + CUDA + 모델(3.9G) + GTCRN(566K)** 전부 들어감 → 자기완결.
입력(samples)·출력(output)만 런타임에 볼륨 마운트.

## 빌드는 왜 160에서 하나
- 노트북 worktree 의 `models/cohere-transcribe-03-2026` 는 **심볼릭링크**라 Docker 가 못 굽음.
- 빌드에 GPU 는 불필요하지만(휠 다운로드일 뿐), 13GB 이미지를 약한 노트북에서 굽고 push 하는 건 비효율.
- 160 에 모델을 실제 디렉토리로 받아두고 거기서 빌드 → 노트북은 코드만 push.

## 1. 빌드 머신(160) 준비 (최초 1회)
```bash
git clone <private-repo> lb-note && cd lb-note
# 모델 실제 디렉토리로 다운로드 (HF, apache-2.0)
uv run hf download CohereLabs/cohere-transcribe-03-2026 \
  --local-dir models/cohere-transcribe-03-2026
# GTCRN 체크포인트 (gitignore, 566KB) — handoff §1 참조
git clone --depth 1 https://github.com/Xiaobin-Rong/gtcrn /tmp/gtcrn_src
mkdir -p models/gtcrn && cp /tmp/gtcrn_src/checkpoints/model_trained_on_dns3.tar models/gtcrn/
```

## 2. 빌드
```bash
./docker/build.sh cu121     # 160용 (torch 2.5/cu121, --extra cu121)
./docker/build.sh cu128     # 171용 (torch 2.7+/cu128, --extra cu128)
```
> cu121/cu128 extras 는 `pyproject.toml` 에 설정 완료, `uv.lock` 에 양쪽 변종 포함됨.
> cu121 은 노트북에서 빌드 검증 완료(14.1GB). cu128 은 **Blackwell GPU(171)에서 런타임 검증 필요**.

## 3. 171 로 이미지 전송 (모델이 이미지 안에 있으니 이미지만 옮기면 됨)
```bash
# A) 레지스트리 (권장)
docker tag lb-note:cu128 ghcr.io/<you>/lb-note:cu128
docker push ghcr.io/<you>/lb-note:cu128        # 160에서
docker pull ghcr.io/<you>/lb-note:cu128        # 171에서

# B) 직접 전송 (레지스트리 없이)
docker save lb-note:cu128 | gzip | ssh <171> 'gunzip | docker load'
```

## 4. 실행 (160/171 공통, 모델 볼륨 불필요)
```bash
IMAGE=lb-note:cu128 ./docker/run.sh \
  tools/run_long_slice10m.py "samples/ax과제회의.m4a" --dereverb --denoise --vad --out output/run1
# 채점
IMAGE=lb-note:cu128 ./docker/run.sh \
  tools/score_frontend.py output/run1/text-*.json --label "fullstack" --baseline 0.529
```

## 트레이드오프 (A안)
- 이미지가 변종당 ~13GB (모델 3.9G 포함). cu121+cu128 = 디스크/레지스트리 ~26GB.
- 모델은 안 바뀌는 고정 릴리스라 재빌드 부담 없음(모델 레이어 캐시 고정 → 코드 수정 시 몇 초 재빌드).
- 모델만 따로 빼고 싶어지면 .dockerignore 에 `models/` 추가 + 런타임 `-v` 볼륨 마운트로 전환 가능.

## cu128(171) 멀티 CUDA extras — ✅ 설정 완료
`pyproject.toml` 에 conflicting extras + extra별 index 매핑 적용, `uv.lock` 재생성 완료:
```toml
[project.optional-dependencies]
cu121 = ["torch>=2.4,<2.6", "torchaudio>=2.5,<2.6"]   # → torch 2.5.1+cu121
cu128 = ["torch>=2.7",      "torchaudio>=2.7"]          # → torch 2.11.0+cu128
[tool.uv]
conflicts = [[{ extra = "cu121" }, { extra = "cu128" }]]
[tool.uv.sources]
torch = [{ index = "pytorch-cu121", extra = "cu121" }, { index = "pytorch-cu128", extra = "cu128" }]
```
- 로컬 dev: `uv sync --extra cu121` (extra 없이는 torch 미설치).
- cu121 검증: `--extra cu121` → torch 2.5.1+cu121 (기존과 동일, 회귀 0) 확인됨.
- **남은 검증(171에서)**: `--extra cu128` 빌드 후 Blackwell 런타임 + WER 동일성(handoff §5: trust_remote_code, GTCRN istft).
