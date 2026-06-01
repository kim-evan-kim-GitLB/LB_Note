# 2026-06-01 서버 배포 · 83분 풀스택 검증 · dev 컨테이너 운영 노트

> 160(RTX 4090/cu121) · 171(RTX PRO 6000 Blackwell/cu128) 두 서버에 lb-note STT 를 배포하고
> 83분 회의 m4a 로 풀스택(WPE+GTCRN+VAD) 파이프라인을 완주·채점한 기록.

## 1. 83분 풀스택 WER 검증 (핵심 결과)

입력: `samples/ax과제회의(클로바노트)_음성파일.m4a` (4992.51s = 83.2분)
실행: `tools/run_long_slice10m.py <m4a> --dereverb --denoise --vad --out output/fullstack`
채점: `tools/score_frontend.py output/fullstack/text-*_slice10m.json --ref answer/ax_tf_클로바.txt --baseline 0.529`

| 서버 | GPU | WER | Δ vs 0.529 | CER | rep | tok_ratio | 전처리 |
|---|---|---|---|---|---|---|---|
| 160 | RTX 4090 (cu121) | **0.4460** | **-0.083** | 0.3252 | 0.0% | 0.927 | wpe+gtcrn+silero |
| 171 | Blackwell (cu128) | **0.4467** | -0.082 | 0.3265 | 0.0% | 0.929 | wpe+gtcrn+silero |

- 9/9 슬라이스 완주(크래시 0), 전처리 4992.5s→4829.5s(무음압축), speech_regions=734.
- 풀스택이 rp1.2 baseline(0.529) 대비 **WER 8.3%p 개선**. 두 서버 0.4460 vs 0.4467 → **GPU/CUDA(cu121 4090 ↔ cu128 Blackwell) 간 parity 확인.**
- 출력 데이터도 양 서버 바이트 동일(samples/answer/models/.env 54파일 SHA256 일치).

## 2. 속도 비교 (4090 vs Blackwell)

GPU ASR 구간(슬라이스 9개)만 비교:

| 서버 | model_load | ASR elapsed | RTFx | VRAM peak |
|---|---|---|---|---|
| 160 (4090) | 2.2s | 14.89s | 335.4 | 6032MB |
| 171 (Blackwell) | 3.1s | 14.11s | 353.8 | 6033MB |

- **GPU ASR 자체는 171(Blackwell)이 ~5% 빠름**(rtfx 353.8 vs 335.4, elapsed 14.1 vs 14.9s). 단형 배치 추론이라 격차는 작음.
- ⚠️ **전체 wall-time 은 GPU 가 아니라 CPU 전처리(WPE dereverb)가 지배** — 83분 오디오 WPE 가 CPU 멀티스레드로 수 분 소요. 따라서 end-to-end 체감 속도는 두 서버가 사실상 비슷.
- 참고: 메모리/구 기록의 rtfx 1.44~2.75 는 로컬 노트북 RTX 3050 Ti 4GB(driver fallback) 값. 데이터센터 GPU(rtfx 335~354)와 직접 비교 불가.

## 3. dev 컨테이너 구성 (양 서버 공통)

- 컨테이너 `lbnote_dev` (160=`lb-note:cu121`, 171=`lb-note:cu128`), `--gpus all`, `--restart unless-stopped`.
- 재구성 2줄: `./docker/dev.sh && ./docker/dev-setup.sh`
  - `dev.sh`: 이름으로 `lbnote_dev` 만 재생성(남의 컨테이너 불간섭). 171 은 `IMAGE=lb-note:cu128 ./docker/dev.sh`.
  - `dev-setup.sh`: 컨테이너 안에 non-root **`evan`** 사용자(uid1000, sudo NOPASSWD) 생성 + Node22 + Claude Code + Codex 를 evan 계정(`~/.npm-global`)에 설치.
- 접속: `docker exec -it -u evan lbnote_dev bash` (반드시 `-u evan`). 인증(claude/codex 로그인)은 사용자가 직접.
- ⚠️ **Python 실행은 `sudo /app/.venv/bin/python`** — venv 인터프리터 실체가 `/root/.local/share/uv/...`(0700) 라 evan 직접 실행 불가. evan 은 NOPASSWD sudo 라 `sudo` 로 우회.

## 4. 데이터 배포 / 백업

- 호스트 백업: 각 서버 `/home/evan/LB_note_data/` 에 `answer/` + `samples/`(합성 wav 포함 704M) + `.env`.
- 컨테이너 `/app` 미러:
  - `samples/*` → 호스트 `~/lbnote/samples`(바인드 마운트, **영구**).
  - `answer/`, `.env` → `docker cp`(**ephemeral**, 재생성 시 소멸 → 백업서 재배포).
  - `models/`(cohere+gtcrn 3.9G) → 이미지 bake.
- ⚠️ **`.env` 경로 주의**: 로컬 `.env` 는 노트북 절대경로(`/home/evan/Claude_workspace/...`)라 컨테이너에선 무효 → 모델/샘플 로드 실패. **컨테이너용 `.env` 는 `/app` 경로로 교정 필수**:
  ```
  COHERE_MODEL_PATH=/app/models/cohere-transcribe-03-2026
  SAMPLES_DIR=/app/samples
  ```
  검증: `sudo /app/.venv/bin/python -c "import sys;sys.path.insert(0,'/app');from src.config import env_status;print(env_status())"` → `cohere_model_exists/samples_dir_exists/gtcrn_model_exists` 모두 True.

## 5. 재생성 후 복구 순서

```bash
cd ~/LB_Note && git pull
./docker/dev.sh && ./docker/dev-setup.sh        # (171: IMAGE=lb-note:cu128 ./docker/dev.sh && ...)
# 데이터 재배포(answer/.env) — /home/evan/LB_note_data 에서:
rsync -a /home/evan/LB_note_data/samples/ ~/lbnote/samples/
docker cp /home/evan/LB_note_data/answer/. lbnote_dev:/app/answer/
# .env 는 /app 경로로 교정해서 주입 (위 §4)
```

## 6. 레포 이전 (2026-06-01)

- 구 `litbig-git/LB_Note` 는 회사 정책 확정 전까지 **폐기 예정** → 관리는 **`kim-evan-kim-GitLB/LB_Note`** (https://github.com/kim-evan-kim-GitLB/LB_Note.git) 로 이전.
- 로컬·160·171 의 `origin` 전부 신 레포로 전환 완료(HEAD 동기).
- ⚠️ **서버 deploy key**: GitHub deploy key 는 키당 1레포 제약이라 구 키(`id_ed25519_lbnote`, 옛 레포 묶임)를 재사용 불가 → 서버마다 **신규 키 `id_ed25519_lbnote_new` 발급**, 신 레포에 read-only deploy key 등록(server-160-4090 / server-171-blackwell). 각 서버 repo 는 `git config core.sshCommand "ssh -i ~/.ssh/id_ed25519_lbnote_new -o IdentitiesOnly=yes"` 로 신 키 사용.
- 레포 내 파일엔 옛 URL 하드코딩 없음(docker 스크립트는 `~/LB_Note` 경로만 참조) → 코드 수정 불필요.

## 관련
- WER/청크 전략: 메모리 `project-stt-chunking-cohere`, `SESSION_STATE.md`
- feat/vad: `docs/feat-vad-handoff.md`
