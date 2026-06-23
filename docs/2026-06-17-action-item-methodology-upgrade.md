# 액션아이템 추출 방법론 고도화 — 회의 결정문서 (2026-06-17)

## 0. 요약(TL;DR)

- **5개 안건 전부 채택하되, 도입 순서를 강제한다: D(precision 안전망) → E(멀티도메인 gold) → A(닫힌 정의확장) → C(코드버그 선수정 후 한정 flag) → B(owner 격리 추론).** precision 게이트와 멀티도메인 측정 없이 A·C부터 켜면 과추출이 회귀 테스트(현재 recall-only)를 그대로 통과해 프로덕션에 나간다.
- **이번 변경은 "프롬프트만 수정·코드 무변경"(criteria §6-4) 원칙이 종료된다.** A는 프롬프트만으로 가능하지만, B(owner 분리 필드)·C(flag 보존 버그)·D(precision 산출)는 `extract_schema.py`·`extract_handoff.py`·`score_extraction.py` 동반 변경이 필수다. 이를 criteria에 명문화한다.
- **검증된 선결 코드버그(C의 차단요인):** `extract_handoff.py:170`이 `flag = None`으로 무조건 초기화하여 LLM이 보낸 flag를 전량 폐기하고, `:187-189`가 병합 시 FLAG_REVIEW를 무조건 해제한다. **프롬프트에 flag 키만 추가하면 flag가 사라진다.** 코드 수정과 동반 머지해야 C가 실제로 동작한다.
- **flag 라벨 3종 분리:** `확인필요`(FLAG_REVIEW, 근거0 환각 — `validate.py:24`에 이미 점유) 유지 / `약함확인`(모호발화) 신설 / `추정`(추론 owner) 신설. 같은 라벨에 몰면 n_flagged 집계·검수 우선순위·precision 분모 분리가 붕괴한다.
- **precision 기준선은 실측에 정박한다.** 현 axfull 골든은 추출 16건/gold 10건이므로 naive precision ≈ **0.63(10/16)**. 패널 일부의 ≥0.7/≥0.8 제안은 baseline부터 fail하므로 기각. hard gate=`confirmed_FP=0` 즉시 잠금 + soft gate=`precision_strict ≥ 0.60`에서 시작해 negative 라벨 보강으로 단계 상향.
- **owner 본필드에 추론값 주입 절대 금지.** owner는 본문에 팀 토큰이 문자적으로 등장할 때만. 추론은 `owner_source='inferred'` + `flag='추정'`으로 격리하고, 추론 근거 evidence_seg가 그 팀 토큰을 실제 포함하는지 결정적 grep 검증.
- `prompt_version` **1.3 → 1.4** 승급(미승급 시 산출물 추적 단절).

---

## 1. 안건별 결정 (A~E)

### 안건 A — 정의 확장: "산출물 무관, 확정된 후속 행동"

**결정: 채택. 단 "산출물 유무 무관"의 무한 도출이 아니라 닫힌 조건으로 확장한다.** 확장 조건 = (a)협의·검토·조율의 **대상이 명시**되고 (b)**완료판정이 한 문장으로 떨어질 때만** 액션. 약동사 단독은 제외.

**근거:**
- `extract.ko.md:20-21`의 핵심정의 괄호 `(보통 산출물이 남는다: 코드·문서·테스트·데이터·전달물 등)`가 협의/검토/조율형 배제의 단일 최대 원인. 이 프레임이 S2 협의·S3 승인요청·S4 추가검토·S5 회신을 구조적으로 누락시킨다.
- axenh 미커버 gid4(서버vs로컬 정리·제안)·gid9(케이스확장)는 같은 dev 도메인 안에서도 "검토·정리형"이라 누락(criteria §58) → A 결손은 도메인 무관 구조적 문제.

**반대의견 반영(critic/R2):**
- "산출물 무관"으로 무한히 열면 S2 외교수사("긍정적으로 검토해보겠습니다")·S5 CS("알아보고 회신")·S4("그건 좀 더 봐야겠네요")가 전부 액션화 → 과추출 1.3~1.6배. **따라서 닫힌 화이트리스트로 제한**하고, A 머지 전 D(negatives+precision)를 **반드시 선행**한다(동일 PR에 negatives 라벨링 강제).

**구체 변경 명세:**

1. **핵심정의(`extract.ko.md:20-21`) 교체:**
   ```
   액션아이템 = 회의가 끝난 뒤 누군가 수행하기로 확정된 구체적 후속 행동.
   산출물(코드·문서·데이터)이 남는 행동뿐 아니라 협의·검토·조율·재확인·승인요청·회신처럼
   산출물이 형태로 남지 않는 행동도 포함한다. 판별 기준은 산출물 유무가 아니라
   「회의 후 누가 무엇을 하기로 확정됐는가」다.
   단 (1)완료된 행동 (2)단순 선언·태도 (3)조건부 가정은 제외한다.
   협의·검토·조율형은 "무엇을 검토/협의해서 무엇이 완료되는가"가 한 문장으로 떨어질 때만 액션이며,
   "검토해보겠다/봐보겠다/맞춰보겠다"처럼 대상·완료판정이 없는 약동사 단독은 제외한다.
   ```

2. **추출대상(`extract.ko.md:35`) 동사목록에 추가:** `협의·검토·조율·합의도출·회신·승인요청·구매/계약 진행·점검`.

3. **판별 자가질문(`extract.ko.md:54`) 교체:** `"이걸 그대로 담당자에게 '할 일'로 넘기면, 그 사람이 무엇을 해야 완료라고 말할 수 있는가(=완료판정 가능한가)?"` — "무엇을 만들어 내야 하는지"(산출물 표현) 제거.

4. **few-shot 표(`extract.ko.md:93-`) ✅행 3건 신설:**

| 발화(예시) | 판단 | 이유 |
|---|---|---|
| "거래처와 납기 일정을 협의해서 확정합시다" | ✅ 추출 (text="거래처와 납기 일정 협의") | 협의형 확정 후속행동(대상=납기일정, 완료=일정확정) |
| "법무팀과 계약 조건을 검토 요청드릴게요" | ✅ 추출 (text="법무팀과 계약조건 검토 요청") | 검토형(대상=계약조건 명시) |
| "양 팀 배포 일정을 조율해서 맞추기로" | ✅ 추출 (text="양 팀 배포 일정 조율") | 조율형(완료판정 가능) |

5. **❌행 신설(경계 박제):**

| "그건 한번 검토해보겠습니다" | ❌ 제외 | 약동사 단독(대상·완료판정 없음) |
| "양사 일정 한번 맞춰보죠" | ❌ 제외 | 조율 의사만(대상 불명) |
| "긍정적으로 검토해보겠습니다" | ❌ 제외 | 외교적 수사 |
| "내부 논의해보겠다" | ❌ 제외 | 주체·산출물 불명 |

6. **기존 ❌행 유지:** `B로 가기로`(결정자체)·`포기하지 않기로`(선언)·`이미 처리했다`(완료)·`문제 생기면 그때`(가정) → commitment≠결정 경계 사수.

---

### 안건 B — owner 팀 귀속 추론

**결정: 조건부 채택. owner 본필드에는 명시 팀만. 추론은 `owner_source` 필드로 격리 + `flag='추정'` 강제 + evidence 토큰 grep 검증.**

**근거:**
- EYEL 양식(criteria §9·§39)은 담당=팀/부서가 핵심. 현 정책(`extract.ko.md:68-70`, criteria §4 "본문 명시만/추측 금지")으로는 회의록 owner 칸이 대부분 빈다.
- 본문에 "주제↔팀 결속 발화"(예: "이 건은 구매팀에서")가 1회라도 있으면 같은 주제 액션에 전파하는 것은 "본문 근거 한정"의 확장형이라 추측 금지 원칙과 양립한다.

**반대의견 반영(critic/R2):**
- owner는 Jira 알림이 실제 발송되는 필드 → 오귀속의 신뢰비용이 오탐보다 크다. **"정적 주제→팀 글로서리 자동매핑" 기각**(비개발/벤더/타사 회의에서 오귀속 양산). **본문 앵커 동적매핑만 허용.**
- 1인칭("제가 하겠다")은 화자분리 부재로 계속 null.

**구체 변경 명세:**

1. **`extract_schema.py` ActionItem에 필드 신설(line 62 부근):**
   ```python
   owner_source: str | None = None  # 'explicit' | 'inferred' | None
   ```
   `from_dict`(line 81)에 `owner_source=(data.get("owner_source") or None)` 추가. `to_dict`는 `asdict` 자동.

2. **owner 규칙(`extract.ko.md:68-70`) 3단계로 교체:**
   ```
   owner: 담당.
   (1) 본문에 팀/부서/역할이 문자적으로 명시 → owner=그 값, owner_source="explicit".
   (2) 명시는 없으나 본문 내 "주제↔팀 결속 발화"가 있어 같은 주제 액션에 전파 가능하면
       그 팀 + owner_source="inferred" + flag="추정"(근거 evidence_seg_ids 필수,
       그 seg가 팀 토큰을 실제 포함해야 함).
   (3) 둘 다 불가하면 null. 1인칭("제가 하겠다")은 화자분리 부재로 null 유지.
   정적 주제→팀 글로서리 자동매핑 금지(본문 앵커 없는 추론 금지).
   ```

3. **출력 JSON 예시(`extract.ko.md:85`)에 `"owner_source": null` 추가.**

4. **few-shot 대비행 추가:**
   - `"이건 구매팀에서 견적 받아주세요" → owner="구매팀", owner_source="explicit"`(명시)
   - `"클라우드 비용 정리해 올려주세요"(인프라 소관 회의, 본문에 인프라팀 결속발화 존재) → owner="인프라팀", owner_source="inferred", flag="추정"`

5. **채점(D와 동반):** `owner_accuracy = owner맞은수 / owner채운수`(채운 것만 분모 → 추측 남발 자동 페널티). `inferred` 항목은 별도 모니터 지표. **추론 근거 evidence_seg가 owner 토큰을 실제 포함하는지 grep 결정적 검증을 채점에 추가.**

---

### 안건 C — 모호 발화 처리(flag 캡처)

**결정: 조건부 채택. (1)선결 코드버그 수정 후 (2)별도 라벨 `약함확인`으로 (3)"메타데이터 모호 + CS/벤더 고객약속 회색지대"에만 한정 캡처. 발화 자체가 모호한 약동사 단독·조건부가정은 flag가 아니라 제외.**

**근거:**
- CS(S5)의 "알아보고 회신" 약속을 버리면 고객 약속 미이행 = 실무상 가장 위험한 누락.
- `flag='확인필요'`(FLAG_REVIEW)는 `validate.py:24`·`extract_handoff.py:171-172`에서 이미 "근거0=환각" 전용으로 점유 → 모호발화에 재사용하면 의미 오염.

**반대의견 반영(critic/R2):**
- C를 "발화 모호"까지 확대하면 S4 짧은 지시("검토해서 가져와")·S5 발화 거의 전부가 flag 도배 → 산출물 절반이 재확인 대상 = 자동화 가치 0. **flag 예산 상한 ≤25%를 채점 게이트로 강제**, 초과 시 근거 약한 항목부터 결정적 drop.
- 순수 약동사 단독·조건부가정("문제 생기면 그때")은 flag가 아니라 제외(정밀도 우선 유지).

**구체 변경 명세 — 선결 코드버그(load-bearing):**

1. **`extract_handoff.py:170` 수정** (현재 LLM flag 폐기):
   ```python
   # 현재(버그): flag = None  → LLM이 보낸 '약함확인'이 전량 소실
   AMBIGUOUS_FLAG = "약함확인"; INFERRED_FLAG = "추정"
   flag = it.flag if it.flag in {FLAG_REVIEW, AMBIGUOUS_FLAG, INFERRED_FLAG} else None
   if not valid_ev:
       flag = FLAG_REVIEW  # 근거0 환각은 OR로 합성(덮어쓰지 않고 보존)
   ```

2. **`extract_handoff.py:187-189` 병합 해제 로직 사유 구분** (현재 무조건 해제):
   ```python
   # grounding-flag(FLAG_REVIEW=no_evidence)는 근거 생기면 해제하되
   # semantic-flag(약함확인)·inferred-flag(추정)는 병합돼도 유지
   if tgt.flag == FLAG_REVIEW:
       tgt.flag = None
       n_flagged -= 1
   # 약함확인/추정은 if 밖 — 유지
   ```

3. **`validate.py`에 상수 추가:** `AMBIGUOUS_FLAG = "약함확인"`, `INFERRED_FLAG = "추정"`(FLAG_REVIEW 옆).

**프롬프트 변경:**

4. **출력 JSON(`extract.ko.md:83-87`)에 `flag` 키 추가:** `{"text":"...","owner":null,"owner_source":null,"due":null,"flag":null,"evidence_seg_ids":[12,13]}` — **이것은 위 코드 수정과 동반 머지해야만 효과가 난다.**

5. **회색지대 섹션 신설(`extract.ko.md:55` 뒤):**
   ```
   ### 회색지대 처리(확정 애매)
   명백히 행동이고 대상이 특정되나 확정·합의 강도가 약한 발화
   ("한번 보겠습니다","검토 후 연락드리겠습니다")는 버리지 말고 추출하되 flag="약함확인".
   특히 CS/벤더의 고객·상대 약속(회신·확인·파악)은 추적가치가 누락리스크보다 크므로 캡처한다.
   확정·합의가 분명하면 flag=null. 순수 약동사 단독·조건부가정·완료·선언은 flag가 아니라 제외다.
   ```

6. **few-shot △행 추가:** `"검토 후 연락드리겠습니다" → ✅추출 flag="약함확인"`(행동·대상은 있으나 확정 약함) / `"문제 생기면 그때 보죠" → ❌제외`(여전히 가정).

7. **criteria 문서에 명시:** `약함확인`(모호발화)·`확인필요`(grounding 실패)·`추정`(추론 owner) 3종 라벨은 모두 사람 검토 큐로 가되 사유 코드가 다르며 n_flagged를 사유별 분리 집계.

---

### 안건 D — precision 정량화(주도 안건, 선행 전제조건)

**결정: 채택. A·C보다 먼저 머지. LLM-judge 없이 기존 `_covers` 재사용한 결정적 precision.**

**근거:**
- 현 `score_extraction.score()`는 `n_extracted` 분모가 없어 recall만 산출 → axfull 16건 중 잉여 6건(예: idx7 "칸반/간트 차트", idx12 "프로 환경 양호 시...돌려보기"=**조건부인데도 추출됨**, `extract.ko.md:48` 제외대상이 이미 새는 중)을 무처벌 통과.
- A로 추출 모수를 넓히기 전에 precision 측정 장치가 없으면 과추출이 recall=1.0 녹색을 유지한 채 프로덕션에 나간다.

**반대의견 반영(R2 precision의견):**
- ≥0.7/≥0.8 임계는 현 실측 0.63과 모순 → baseline fail. **hard gate `confirmed_FP=0` 즉시 + soft gate `precision_strict≥0.60` 시작.**

**구체 변경 명세:**

1. **`eval/gold_actionitems.json`에 `negatives[]` 블록 신설**(items와 동형 스키마, `_covers` 재사용):
   ```json
   "negatives": [
     {"nid":1,"text":"B로 가기로 결정(방침자체)","keywords":[["B"],["가기로","결정"]],"required":[0,1],"min_match":2,"kind":"decision"},
     {"nid":2,"text":"포기하지 않기로(선언)","keywords":[["포기"]],"required":[0],"min_match":1,"kind":"declaration"},
     {"nid":3,"text":"필요하면 추가(조건부)","keywords":[["필요하면"]],"required":[0],"min_match":1,"kind":"conditional"},
     {"nid":4,"text":"이미 처리했다(완료)","keywords":[["이미 처리"]],"required":[0],"min_match":1,"kind":"completed"},
     {"nid":5,"text":"프로 환경 양호 시 돌려보기(조건부, axfull 잉여)","keywords":[["양호"],["돌려"]],"required":[0,1],"min_match":2,"kind":"conditional"}
   ]
   ```
   **주의:** `required` 토큰은 변별력 높게 좁게(`포기`/`필요하면`/`이미 처리`/`양호`) — substring 오매칭으로 정상항목을 FP 처리하는 것 방지.

2. **`score_extraction.py`에 precision 추가**(`by_gid`의 `matched_item` 인버전, 신규 LLM 의존 0):
   ```python
   def score_precision(extracted, gold) -> dict:
       texts = _item_texts(extracted)
       positives = gold.get("items", [])
       negatives = gold.get("negatives", [])
       n_ext = len(texts)
       tp = sum(1 for t in texts if any(_covers(t, p) for p in positives))
       fp = sum(1 for t in texts
                if not any(_covers(t, p) for p in positives)
                and any(_covers(t, neg) for neg in negatives))
       unmatched = n_ext - tp - fp
       return {
           "precision_strict": round(tp / n_ext, 4) if n_ext else 0.0,
           "confirmed_FP": fp,
           "fp_rate": round(fp / n_ext, 4) if n_ext else 0.0,
           "unmatched_rate": round(unmatched / n_ext, 4) if n_ext else 0.0,
           "n_extracted": n_ext,
       }
   ```
   `flag='약함확인'` 항목은 precision_strict 분모에서 분리 집계(C의 캡처가 precision을 깎지 않게).

3. **`tests/test_score_extraction.py` 회귀 락 추가:** `axfull confirmed_FP==0`(hard), `axfull precision_strict ≥ 0.60`(soft), `axfull precision ≥ axenh precision`(대칭 방향 불변식).

---

### 안건 E — 회의 유형 일반화

**결정: 채택. 단 프롬프트 중립화만으론 공허 — gold 멀티도메인 신설이 동반돼야 실측 가능.**

**근거:**
- `test_score_extraction.py`는 단일 `GOLD_PATH`에 묶이고 axfull/axenh 둘 다 같은 ax 회의 → S2~S5에서 무슨 과추출·오귀속이 나도 회귀가 침묵.
- 편향의 실체 = (1)핵심정의 괄호 예시 (2)few-shot 12행 전부 dev (3)gold 10건 단일회의(슬랙/모델/페이즈1).

**구체 변경 명세:**

1. **`GOLD_PATH` 단일경로 → `eval/gold/*.json` 글롭화.** `score_extraction`을 멀티-gold로 확장.
2. **미니 gold 신설**(유형당 5~8 positive + negatives, **도메인 토큰이 아니라 행동·소관 토큰 위주**로 작성해 개발편향 재발 방지):
   - `eval/gold/gold_s2_vendor.json` (예: "견적서 6/30까지 회신", owner_keywords=영업/구매/협력사)
   - `eval/gold/gold_s3_ops.json` (예: "구매 품의서 작성·승인 상신", owner_keywords=구매/총무/인사)
   - `eval/gold/gold_s4_exec.json` (예: "비용 영향 추가 검토 후 보고")
   - `eval/gold/gold_s5_cs.json` (예: "고객 환불요청 follow-up 회신", owner_keywords=CS/지원)
3. **few-shot 도메인 중립화:** dev 편중 6행 중 2행을 비개발 대비행으로 교체(A의 협의/검토/조율 ✅행과 S5 이슈해결 vs 추상다짐 ❌행 활용).
4. **합격선을 도메인별로 분해:** dev(axfull)=1.0 유지, 신규 S2~S5 ≥0.8 착수 후 사용자 확정. dev=1.0인데 타 도메인 0인 사각을 가시화.
5. **criteria §16/§5에 비개발 실추출 예시 병기**하여 "유형 불문" 문서화.

---

## 2. 시나리오 진단표 (S1~S5)

| 시나리오 | 현 프롬프트 동작 | 취약점 | 개선안(추가 few-shot 발화예시 포함) |
|---|---|---|---|
| **S1 개발 내부**(baseline=axfull) | recall=10/10 통과하나 16건 추출(잉여 6건). idx12 "프로 환경 양호 시 돌려보기"=조건부인데도 추출(`L48` 제외가 이미 샘). owner 거의 빔(1인칭 null). | precision 미측정→과추출 무처벌. 검토형 gid4·9 axenh 미커버. | **D**로 잉여 6건 가시화, idx12·idx7을 negative 박제. **A**로 검토·정리형 회수("운영 장단점 정리해 제안"✅). **B**로 본문 결속발화 있으면 owner 추론(flag="추정"). |
| **S2 외부 업체** | "양사 협의"·"검토 후 연락"이 산출물 기준으로 제외. "견적 6/30 회신"은 due만 잡히고 owner null. | A 결손 정면사례. owner 양사 구분 불가(B). vendor gold 0건(E). | **A**: ✅"양사 납기 일정 협의"(조율형). **C**: △"견적서 회신드리겠습니다"→flag="약함확인". **B**: owner="협력사/영업"(본문 결속 시 inferred). **E**: vendor gold 신설. ❌"긍정적으로 검토"(외교수사). |
| **S3 비개발 실무** | 승인요청·조율형 제외(A). owner는 "구매팀" 명시만, 조율 소관(HR) 추론 불가(B). | EYEL식 팀 귀속(criteria §9)이 핵심인데 owner 빔. dev gold만 있어 미측정(E). | **A**: ✅"팀장 승인 요청"·"채용 일정 인사팀과 조율". **B**: 채용→HR, 계약→법무, 구매→구매팀(본문 앵커 시 inferred+추정). **E**: ops gold. **D**: "검토하겠다"(다짐) vs "품의서 작성"(액션) negative 박제. |
| **S4 경영/임원 보고** | "그건 좀 더 봐야겠네요"(추가검토 지시)가 검토형이라 약하게 잡힘. 지시 수신팀 null. | A 결손. 결정+실행 혼재 발화에서 실행부 누락 위험(`L39-40`). C 과flag 위험(임원 지시는 대개 확정). | **A**: ✅"비용 영향 추가 검토 후 보고"(검토형). **B**: 지시 수신팀 추론(임원회의는 소관 분명). **C 절제**: 짧은 지시 "검토해서 가져와"(대상 불명)는 flag 아닌 제외. ❌"문제 생기면 재검토". |
| **S5 고객 CS** | "알아보고 회신" 약속이 모호로 제외 → 고객 약속 미이행 리스크(가장 위험). owner null. | C 결손 정면사례. cs gold 0건(E). "잘 챙기겠습니다"(다짐) vs "원인 파악해 해결"(액션) 경계 불명. | **C 최우선**: "확인 후 연락"→flag="약함확인" 캡처. **B**: 재현·수정→SW2팀, 회신→CS팀(inferred). **A**: "이슈 해결·회신" 정의 포함. **D**: "불편 드려 죄송, 잘 챙기겠습니다"→❌ negative 박제. **E**: cs gold. |

---

## 3. precision 측정 설계

**오탐 라벨 자료구조** (`eval/gold_actionitems.json` 및 `eval/gold/*.json`에 `negatives[]` 신설):
- 원소 = `{nid, text(설명), keywords(그룹 OR), required, min_match, kind}`. `kind ∈ {declaration|decision|conditional|completed|chitchat|borderline}`.
- positives와 **동형 스키마** → 기존 `_covers` 그대로 재사용, LLM-judge 불필요, 같은입력→같은점수(docstring 보장) 유지.
- axfull 잉여 6건 중 idx12("프로 환경 양호 시 돌려보기"=conditional)·idx7("칸반/간트 차트"=borderline)를 1차 negative 라벨 후보로 박제. **required 토큰은 변별력 높게 좁게**(substring 오매칭 방지).

**precision 정의** (추출→gold 역집계 3분류):
- 각 추출 텍스트를 (a)어떤 positive를 cover → **TP기여**, (b)아니면서 어떤 negative를 match → **confirmed_FP**, (c)둘 다 아님 → **unmatched**(병합변형/미라벨 회색).
- `precision_strict = TP기여추출수 / 전체추출수`, `fp_rate = confirmed_FP / 전체추출`, `unmatched_rate = unmatched / 전체추출`.
- 한 positive를 2개 이상 추출이 cover하면 "중복추출"로 별도 카운트(병합규칙 위반 신호).
- `flag='약함확인'` 항목은 precision_strict 분모에서 분리(lenient/strict 이원 집계).

**회귀 게이트 임계** (3중):
1. `axfull recall = 1.0` 불변(기존 test 유지).
2. `confirmed_FP = 0` — **hard gate**(라벨된 명백 비액션은 절대 추출 금지).
3. `precision_strict ≥ 0.60` AND `unmatched_rate ≤ 0.30` — **soft gate**. A 도입 시 일시 완화 후 negative 보강으로 회복.
4. 대칭 방향 불변식 `axfull precision ≥ axenh precision` 추가.
- 위반 시 **가장 약한 근거 항목부터 결정적 drop**.

**axfull recall 재기준선 처리:**
- A로 추출 모수가 바뀌면 현 `extracted_axfull.golden.json`(16건)의 하드코딩 assertion(`covered_gids==range(1,11)`, axenh `missing==[4,6,9]`)이 깨진다.
- **golden fixture를 freeze하지 말고 "재채점·재승격"으로 전환.** 새 프롬프트로 1회 추출한 산출을 **승격조건 `recall=1.0 AND confirmed_FP=0`으로만** 새 golden 승격. 늘어난 추출은 negatives로 흡수해 precision 기준선을 같이 박제. axenh(현 7/10)도 동일 재채점 후 신 기준선 락.
- **정량 영향 추론:** A 정의확장 → recall +0.1~0.3(axenh 7→8~9/10), precision은 라벨 보강 전 -0.10~0.20 하락 예상(추출 1.3~1.6배). 따라서 A·D 동시 착수 필수(D의 negatives가 새 오탐을 즉시 라벨링 못하면 precision은 분모만 늘고 측정 불능).

---

## 4. owner 팀 귀속 정책

**허용 범위(3단계):**
1. **명시**: 본문에 팀/부서 토큰 문자등장 → `owner=그 값`, `owner_source="explicit"`, flag=null.
2. **본문 앵커 추론**: 본문 내 "주제↔팀 결속 발화"(예: "이 건은 구매팀에서")가 1회 이상 등장 → 같은 주제 액션에 전파 → `owner=팀`, `owner_source="inferred"`, `flag="추정"`. **근거 evidence_seg 필수**.
3. **불가**: null. 1인칭("제가 하겠다")은 화자분리 부재로 null.

**가드레일(추측 환각 금지선):**
- **owner 본필드에 추론값 주입 절대 금지** — owner는 Jira 알림 발송 필드, 오귀속 신뢰비용 > 오탐. 추론은 `owner_source="inferred"`로만 격리.
- **정적 주제→팀 글로서리 자동매핑 금지.** 회의 본문에서 정의된 동적 매핑만(비개발/벤더/타사 오귀속 방지).
- **결정적 grep 검증을 채점에 추가:** 추론 근거 `evidence_seg_id`가 그 팀 토큰을 실제 포함하는지 대조. 미포함 시 추론 무효.
- 채점: `owner_accuracy = 맞은수 / 채운수`(채운 것만 분모 → 추측 남발 자동 페널티). explicit만 정확도 채점, inferred는 별도 모니터.

---

## 5. 모호 발화 처리 정책

**flag 3종 분리 운용:**
- `확인필요`(FLAG_REVIEW, `validate.py:24`): grounding 실패(근거0 환각). **기존 점유 유지.**
- `약함확인`(신설): 모호발화 회색지대 캡처.
- `추정`(신설): 추론 owner.

**`약함확인` 운용 규칙:**
- 적용 대상 = (1)행동·대상은 명확하나 확정 강도가 약한 발화 (2)CS/벤더 고객·상대 약속(추적가치 > 누락리스크). 메타데이터(owner/due) 불확실에도 적용.
- 비적용(제외 유지) = 순수 약동사 단독("검토해보겠다")·조건부가정("문제 생기면 그때")·완료·선언.

**남발 억제책:**
- **flag 예산 상한 ≤25%를 채점 게이트로 강제.** 초과 시 근거 약한 항목부터 결정적 drop.
- **선결 코드버그 수정 필수**(§1-C): `extract_handoff.py:170` flag 보존, `:187-189` semantic-flag 병합 유지. 안 고치면 프롬프트만 바꿔도 flag 소실.
- n_flagged를 사유별(확인필요/약함확인/추정) 분리 집계.

---

## 6. 회귀 리스크 및 가드레일

| 리스크 | 근거(검증됨) | 가드레일 |
|---|---|---|
| **golden fixture freeze 붕괴** | `test_axfull_golden_full_recall`의 `covered_gids==range(1,11)`, axenh `missing==[4,6,9]` 하드코딩. A로 모수 바뀌면 fail. | freeze 대신 재채점·재승격(승격조건 recall=1.0 AND confirmed_FP=0). |
| **recall=1.0의 거짓 안전감** | `score()`에 n_extracted 분모 없음 → 잉여 6건 무처벌. | A·B·C 도입 PR마다 3중 게이트(recall=1.0 + precision 하한 + negatives 무위반) 동시 통과. |
| **방향 불변식 역전** | `test_verdict_direction_holds`(axfull≥axenh). A확장 후 axenh gid4·6·9 회수 시 격차 축소. | precision에도 `axfull≥axenh` 대칭 불변식 추가. |
| **코드 무변경 원칙 종료** | criteria §6-4 "프롬프트만 수정". B·C·D는 schema·handoff·scorer 동반 변경 필수. | criteria에 "이번 변경=프롬프트+schema+handoff+scorer 동반" 명문화, 회귀 범위를 코드까지 확장. |
| **merge 병합이 신규 flag 삼킴** | `extract_handoff.py:179-189` 정규화키 병합이 flag 해제. | 병합 로직에 사유별 flag 보존(약함확인/추정 유지) 테스트 추가. |
| **prompt_version 미승급 추적 단절** | `extract.ko.md:3` prompt_version: 1.3. 산출물(actionitems.json)이 어떤 정의로 뽑혔는지 추적 불가. | 모든 정의·필드 변경 후 **1.3 → 1.4 승급** 필수. |
| **측정 없는 일반화** | `GOLD_PATH` 단일, S2~S5 무측정. | `eval/gold/*.json` 글롭화 + S2~S5 미니 gold 신설, 유형별 회귀락. |

**도입 순서 강제(가드레일 종합):**
> **D**(negatives+precision 결정적 산출) → **E**(유형별 gold 신설) → **A**(닫힌 정의확장) → **C**(코드버그 선수정 후 한정 flag) → **B**(owner_source 격리). 각 단계마다 `axfull recall=1.0` AND `axfull confirmed_FP=0`을 회귀 락으로 잠근 뒤 다음 안건 진행.

---

## 7. 미해결 / 후속

- **precision soft gate 최종 임계 확정**: 잠정 0.60 시작 → negatives 라벨 보강 후 사용자 확정. (현 실측 baseline ≈ 0.63.)
- **S2~S5 미니 gold 합격선 확정**: ≥0.8 착수 후 1차 측정값으로 사용자 확정.
- **flag 예산 상한 수치 확정**: 20% vs 25% — 1차 측정 후 결정.
- **EYEL PDF 본문 직접 검증 미완**: `samples/EYEL-S3000ABR 데모시연 회의록.pdf`가 CID 임베디드 폰트라 디코딩 실패. 본 문서의 EYEL 양식 근거는 criteria §4·§7 명문화 기록 사용. 양식 세부 검증 필요 시 poppler-utils 설치 또는 venv PDF 라이브러리 추가 후 재확인 권장.
- **owner inferred 채움률 운영 모니터링**: 도입 후 inferred 비율·오귀속률 추적 지표 정의 필요.
- **`gold_negatives` 라벨링 1회 사람 작업**: criteria §6 경계라벨링 절차에 "negative 라벨" 단계 추가.

---

## 8. 구현·검증 결과 (2026-06-17 실행)

**구현 완료(코드+프롬프트+문서+테스트):**
- `src/postprocess/validate.py` — `AMBIGUOUS_FLAG="약함확인"`·`INFERRED_FLAG="추정"`·`KNOWN_FLAGS` 신설.
- `src/postprocess/extract_schema.py` — `ActionItem.owner_source` 필드 + `from_dict` 반영.
- `src/postprocess/extract_handoff.py` — **flag 폐기 버그 수정**(LLM flag 보존, grounding 만 해제),
  추론 owner→'추정' 격리, `n_flagged` 사유별 분리 집계(`flag_breakdown`) + JSON/MD 노출.
- `src/postprocess/score_extraction.py` — `score_precision()`(결정적 precision/confirmed_FP),
  `load_gold_dir()`(멀티도메인 글롭). `score_file()` 가 precision 동반 반환.
- `src/postprocess/web_contract.py` — actionItems 계약에 `owner_source` 노출.
- `prompts/extract.ko.md` — **extract-ko-1.4**(A 정의확장 / B owner 3단계 / C 회색지대 flag /
  E 비개발·벤더·CS few-shot). 코드의 `load_extract_prompt_version()` 가 1.4 로 인식 확인.
- `eval/gold_actionitems.json` — `negatives[]`(오탐 라벨) 5건.
- `eval/gold/*.json` — 멀티도메인 시드 4종(S2~S5) + README(SEED 명시).
- `docs/2026-06-09-action-item-criteria.md` — §9 v1.4 갱신 addendum.
- `tests/test_score_extraction.py` — precision 회귀 락 + 멀티도메인 well-formedness(총 11 케이스).

**회귀·정밀도 게이트(전부 green):**
- `tests/test_score_extraction.py` 11/11, `tests/test_extract_actions_endpoint.py` 4/4 (venv).
- 동결 골든 baseline 박제: axfull recall=1.0·precision_strict=0.8125·confirmed_FP=1(idx12 조건부 누수,
  v1.4 재추출 목표=0) / axenh recall=0.7·precision_strict=0.8·confirmed_FP=0. precision 방향 불변식 axfull≥axenh.
- collect 단위 검증: 약함확인 보존·추론 owner→추정·근거0→확인필요, 사유별 집계 정상.

**시나리오 프로브(S1~S5, 신선한 추출 에이전트가 v1.4 프롬프트만 적용 → 결정적 collect):**
- 협의·검토·조율·회신·승인요청형 **포착**(S2 납기협의, S3 품의·승인/채용조율, S4 비용검토·보고, S5 환불회신).
- 회색지대 **`약함확인` 캡처**(S2 견적 회신, S5 "알아보고 연락").
- owner **추론**(S1 인프라팀 inferred+추정) / **명시**(S3 인사팀 explicit).
- **제외 정확**(외교수사 "긍정적으로 검토", 약동사 단독 "맞춰보죠/봐야겠네요", 방침 "B로 결정",
  추상다짐 "잘 챙기겠습니다", 조건부 "상황 봐서 나중에") → 전 시나리오 **confirmed_FP=0**.
- (시드 gold recall<1.0 은 시드 템플릿이 프로브 transcript 와 미짝의 정상 아티팩트 — 정성 검증용.)

**v1.4 라이브 1차 테스트 발견 → 교정(2026-06-18):**
- 발견: `약함확인` **과적용**(axfull 4/9=44%, 예산 25% 초과). 담당/기한만 미정인 **확정 액션**(클라우드 비용
  제안=gid4, 마일스톤 정리=gid8)에도 flag 가 붙어 검토 큐가 부풀고 유용성 저하.
- 교정: 프롬프트 "회색지대" 규칙을 **"할지 말지 자체가 애매한 경계 발화"로 한정**(담당/기한 미정은 flag=null),
  대비 few-shot 2행 추가, 자가점검 문구(1/4 이하) 삽입. `score_precision()` 에 `ambiguous_rate`·`over_flag_budget` 관찰값 추가.
- 효과(axfull 재추출): 약함확인 **44% → 6%**(1/17), 예산 내. 부수효과로 recall **1.0(10/10)**·confirmed_FP **0**
  동시 충족(승격 기준 충족 런). 남은 1건("주관 평가 의견 제공")은 실제로 약한 약속이라 적절.

**후속(미실행, 사용자 판단 — 비용/베이스라인 변경 동반):**
- axfull/axenh **전체 회의 라이브 재추출**(`run_extract.py emit→fill→collect`, backend=agent_cli)로
  골든 픽스처 **재승격**(승격조건 recall=1.0 AND confirmed_FP=0). 현재는 v1.3 동결 픽스처 유지.
- S2~S5 실제 도메인 transcript 확보 후 시드 gold 확정·도메인별 합격선(≥0.8) 측정.

**근거 파일(절대경로):**
- `/app/prompts/extract.ko.md` (L20-21 핵심정의 괄호=A 결손 단일 최대 원인 / L48 조건부 제외 / L68-70 owner 명시한정 / L83-87 출력 JSON flag 키 부재)
- `/app/src/postprocess/extract_handoff.py` (L170 `flag=None` 초기화 버그 / L171-172 FLAG_REVIEW grounding전용 / L187-189 병합 시 무조건 해제 — **C 선결 차단요인, 검증됨**)
- `/app/src/postprocess/validate.py` (L24 `FLAG_REVIEW="확인필요"` 점유)
- `/app/src/postprocess/extract_schema.py` (L62 evidence_seg_ids / L63 flag 필드 / L81 owner — owner_source 신설 위치)
- `/app/src/postprocess/score_extraction.py` (L55-78 `score()` recall-only, n_extracted 분모 부재 / L27-37 `_covers` 재사용 가능)
- `/app/eval/gold_actionitems.json` (10건 positives, negatives[] 부재)
- `/app/tests/test_score_extraction.py` (L34-48 하드코딩 `covered_gids==range(1,11)`·`missing==[4,6,9]` / L24 단일 GOLD_PATH)
- `/app/tests/fixtures/extracted_axfull.golden.json` (16건 — idx7 칸반/간트, idx12 "프로 환경 양호 시 돌려보기"=조건부 누수 = naive precision 10/16≈0.63)
- `/app/docs/2026-06-09-action-item-criteria.md` (§4 owner 명시한정 / §6-4 코드무변경 원칙 / §9 EYEL 팀귀속 / §62 axfull=1.0 회귀락)
