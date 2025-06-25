# core/confirmation.py

"""
LTF(저주기) 캔들에서 ‘반등·리젝션’을 간단히 감지하는 헬퍼.

LONG  : 마지막 캔들 종가가 직전 high 돌파 && 저가가 직전 저가보다 높을 때
SHORT : 마지막 캔들 종가가 직전 low  하향 && 고가가 직전 고가보다 낮을 때

body_ratio (기본 0.45) ─ 캔들 실체(|close-open|) 가 전체 길이
(|high-low|) 에서 차지하는 최소 비율.  너무 짧은 핀바·노이즈 캔들 제외용.
"""

from __future__ import annotations
import pandas as pd

def confirm_ltf_reversal(
    df: pd.DataFrame,
    direction: str,
    *,
    body_ratio: float = 0.45,
) -> bool:
    if df is None or len(df) < 3:          # 캔들 2개 이상 필요
        return False

    last, prev = df.iloc[-1], df.iloc[-2]

    rng  = last["high"] - last["low"] or 1e-9   # div-by-zero 방지
    body = abs(last["close"] - last["open"])
    if body / rng < body_ratio:                 # 실체가 너무 짧으면 무시
        return False

    if direction == "long":
        return (last["close"] > prev["high"]) and (last["low"] > prev["low"])
    else:  # short
        return (last["close"] < prev["low"]) and (last["high"] < prev["high"])