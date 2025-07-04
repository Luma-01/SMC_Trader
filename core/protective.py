# core/protective.py

import pandas as pd
from typing import Optional, Dict


def _is_swing_low(series, idx: int, span: int) -> bool:
    """idx 캔들이 좌우 span 개보다 모두 낮으면 True"""
    low = series[idx]
    return low == min(series[idx - span: idx + span + 1])


def _is_swing_high(series, idx: int, span: int) -> bool:
    high = series[idx]
    return high == max(series[idx - span: idx + span + 1])


# span 기본값 3 → **2**  ⇒ 좌우 2개만 넘으면 스윙으로 인정 (완화)
def get_protective_level(df: pd.DataFrame,
                         direction: str,
                         lookback: int = 30,
                         span: int = 2) -> Optional[Dict]:
    """
    최근 LTF 스윙 로우(롱) / 스윙 하이(숏) 를 보호선으로 반환
    • lookback 구간 안에서 가장 마지막 스윙 포인트를 사용
    """
    if len(df) < span * 2 + 1:
        return None

    df = df.reset_index(drop=True)
    lows  = df['low'].tolist()
    highs = df['high'].tolist()

    rng = range(len(df) - span - 1, max(len(df) - lookback - span, span) - 1, -1)

    for i in rng:
        if direction == "long" and _is_swing_low(lows, i, span):
            return {"protective_level": lows[i], "swing_time": df['time'][i]}
        if direction == "short" and _is_swing_high(highs, i, span):
            return {"protective_level": highs[i], "swing_time": df['time'][i]}
    return None

# ────────────────────────────────────────────────
# 기존 호출부 호환용 래퍼
#   ↪︎ 내부에서 그대로 generic 함수를 부릅니다
# ────────────────────────────────────────────────

def get_ltf_protective(df: pd.DataFrame,
                       direction: str,
                       lookback: int = 30,
                       span: int = 2) -> Optional[Dict]:
    return get_protective_level(df, direction, lookback, span)
