# STT 인식 정확도 판단 — ax 과제회의 음성

- 작성일: 2026-06-04
- 대상 음원: `samples/ax과제회의(클로바노트)_음성파일.m4a` (83.2분, speech 90%)
- reference: `answer/ax_tf_클로바.txt` (Clova Note STT, 화자 헤더 제거 — **ground truth 아님**)
- 근거 데이터: `output/normalized/comparison.md`, `output/score-*.md`, `output/transcript-ax_vad_chunk.txt`
- 관련 Jira: WDLABD2411-531 (LB NOTE Phase 1)

## 1. 정량 판단 — WER 절대값은 "오인식률"이 아니다

| run | enhancers | segmentation | WER | CER | hyp/ref 토큰비 | rep | RTFx | VRAM |
|---|---|---|---|---|---|---|---|---|
| single_call | none | single(1) | 0.420 | 0.302 | 0.942 | 0.0 | 332 | 20.8 GB |
| slice10m | none | slice(9) | 0.425 | 0.310 | 0.930 | 0.0 | 385 | 6.0 GB |
| **vad_chunk** | none | silero_vad(213) | **0.417** | 0.304 | 0.944 | 0.0 | 52 | **4.0 GB** |
| single_call_enh | wpe+gtcrn+silero | single(1) | 0.424 | 0.300 | 0.964 | 0.0 | 10 | 20.4 GB |
| vad_chunk_enh | wpe+gtcrn | silero_vad(208) | 0.427 | 0.307 | 0.951 | 0.0005 | 10 | 4.0 GB |

판단 근거 3가지:

- **(a) CER(0.30) ≪ WER(0.42)** — 문자 단위 일치율 70% vs 어절 단위 58%. 이 격차는 오류가 *내용 오인식*이 아니라 **띄어쓰기·조사/어미·발화 단위 경계** 차이에 몰려 있다는 한국어 STT의 전형적 신호. "다른 단어를 들었다"가 아니라 "같은 말을 다르게 끊었다"가 다수.
- **(b) hyp/ref 토큰비 0.944, repetition 0** — 누락·long-form collapse·반복 hallucination이 사실상 없음. 음성 전체를 안정적으로 끝까지 받아씀.
- **(c) reference가 Clova STT** — 0.42는 "Cohere가 42% 틀렸다"가 아니라 "두 STT 엔진이 42% 어절에서 불일치". 둘 다 같은 음원을 받아쓴 결과의 차이이며, 현재 기준점(Clova)이 곧 정확도 천장.

## 2. 정성 판단 — 실제 구간 대조에서 드러난 불일치 성격

transcript를 Clova와 대조하면 불일치 대부분이 3가지 비-치명 유형:

1. **외래어/고유명사 (양쪽 다 오인식, 음원 한계)**
   - `ChatGPT`: Clova "채집 피키" vs Cohere "채찌피티"(오히려 근접)
   - `Qwen`: "환 / Quan / QWEN" 혼재 · `Whisper`: "위스퍼 / 위스펀 / 위시펀" · `SOTA`, `Cohere/코히얼`, `Opus/Sonnet`
   - → audio_quality의 **cutoff 5922Hz(≈6kHz 대역제한)** 가 외래어 자음 변별에 필요한 고역을 잘라낸 직접 결과.
2. **간투사/비유창성** — Cohere가 "그, 어, 뭐, 아 네네" 등 filler를 더 많이 남김(Clova는 정리). 토큰 차이를 키우나 의미 손실 아님.
3. **화자 경계 병합** — vad_chunk는 diarization이 없어 화자 전환부에서 어절 순서가 Clova(참석자 헤더 보유)와 어긋남.

의미 전달 자체는 양쪽 거의 동일 — 회의 내용(로컬 LLM, 모델 선정 Qwen vs Cohere, AI Hub 평가셋, 액션아이템 정의 등)이 정확히 재현됨.

## 3. 결론

- **실질 인식 품질은 양호.** 0.42 WER은 모델 결함이 아니라 ① 비교 기준이 또 다른 STT, ② 자발화 간투사, ③ 6kHz 대역제한 음원의 외래어 — 세 요인의 합산물.
- **5종 방식 간 WER 차이는 평탄**(0.417~0.427, 폭 0.010). 인식 정확도 레버로 청킹/음향향상은 무의미. 표준 파이프라인으로 **vad_chunk** 채택이 합리적(최저 WER + 최저 VRAM 4GB + 타임스탬프 가능). 음향향상은 net-negative 재확인.
- **정확도를 더 끌어올릴 진짜 레버** (청킹이 아님):
  1. **신뢰할 ground truth 확보** — 현재 Clova 기준은 천장. 소량 수동 정답셋(또는 AI Hub 정답지)으로 절대 WER 재측정 필요.
  2. **외래어/도메인 용어 보정** — 프롬프트 바이어싱·후처리 사전(ChatGPT/Qwen/Whisper/Cohere/SOTA 등 고정 표기).
  3. **대역제한 원음 품질** — 6kHz cutoff는 녹음 단계 문제. 후단 음향향상으로는 복원 불가(net-negative 확인됨), 녹음 경로 개선이 근본 레버.
