"""결정적 반복-환각 collapse (P2 백스톱).

ASR 디코딩 루프가 같은 토큰/짧은 구를 수십~수백 번 반복하는 환각("좀, 좀, 좀…")을
세그먼트 텍스트 수준에서 결정적으로 접는다. repetition_penalty(디코딩)와 별개의 2층 방어:
디코딩 분포를 바꾸지 않으므로 정상 발화는 바이트 단위로 불변(임계 미만은 no-op).

원본을 파괴하지 않고 `keep` 부만 남기며, 접힌 세그먼트에는 호출부가 `확인필요` 플래그와
원문을 meta 에 보존하도록 (cleaned_text, collapsed) 를 반환한다. 음원·LLM 무관, stdlib 만.
"""
from __future__ import annotations

# 최장 매칭 반복 단위(토큰 수). 구-단위 루프("네 알겠습니다 네 알겠습니다…")까지 포착.
MAX_UNIT = 6
# 연속 반복을 이 횟수까지는 정상으로 허용("하 하 하" 류). 초과분만 접는다.
DEFAULT_MAX_REPEAT = 3
# 접은 뒤 남길 단위 반복 수.
DEFAULT_KEEP = 1


def collapse_repetitions(
    text: str,
    *,
    max_repeat: int = DEFAULT_MAX_REPEAT,
    keep: int = DEFAULT_KEEP,
) -> tuple[str, bool]:
    """연속 반복하는 1..MAX_UNIT 토큰 단위를 `keep` 부로 접는다.

    max_repeat 이하 연속 반복은 보존(정상 발화의 강조/추임새). 초과 시에만 접고
    collapsed=True 를 돌려준다. 공백 토큰화 — 구두점은 토큰에 붙은 채 비교(예: "좀," 단위).

    반환: (collapsed_text, collapsed). 접지 않았으면 원문과 False(no-op 보장).
    """
    tokens = text.split()
    n = len(tokens)
    if n < 2:
        return text, False

    out: list[str] = []
    i = 0
    collapsed = False
    while i < n:
        best: tuple[int, int] | None = None  # (unit_len, repeat_count)
        for unit_len in range(1, min(MAX_UNIT, n - i) + 1):
            unit = tokens[i:i + unit_len]
            reps = 1
            j = i + unit_len
            while j + unit_len <= n and tokens[j:j + unit_len] == unit:
                reps += 1
                j += unit_len
            if reps > max_repeat:
                # 가장 많은 토큰을 덮는 단위 선택(동률이면 먼저 찾은 짧은 단위).
                coverage = unit_len * reps
                if best is None or coverage > best[0] * best[1]:
                    best = (unit_len, reps)
        if best is not None:
            unit_len, reps = best
            unit = tokens[i:i + unit_len]
            out.extend(unit * keep)
            i += unit_len * reps
            collapsed = True
        else:
            out.append(tokens[i])
            i += 1

    if not collapsed:
        return text, False
    return " ".join(out), True
