# core/utils.py (또는 적절한 위치)

import pandas as pd
from typing import Tuple

def refined_premium_discount_filter(htf_df: pd.DataFrame, ltf_df: pd.DataFrame, direction: str, window: int = 50) -> Tuple[bool, str, float, float, float]:
    if htf_df.empty or ltf_df.empty or 'close' not in ltf_df.columns:
        return False, "데이터 부족", None, None, None

    htf_recent = htf_df.tail(window)
    htf_high = htf_recent['high'].max()
    htf_low = htf_recent['low'].min()
    mid_price = (htf_high + htf_low) / 2
    current_price = ltf_df['close'].dropna().iloc[-1]

    ote_high = htf_low + 0.79 * (htf_high - htf_low)
    ote_low = htf_low + 0.618 * (htf_high - htf_low)

    if direction == 'long':
        if current_price > mid_price:
            return False, f"LONG인데 프리미엄 ({current_price:.2f} > {mid_price:.2f})", mid_price, ote_low, ote_high
        if not (ote_low <= current_price <= ote_high):
            return False, f"LONG인데 OTE 아님 ({current_price:.2f} ∉ {ote_low:.2f} ~ {ote_high:.2f})", mid_price, ote_low, ote_high
    elif direction == 'short':
        if current_price < mid_price:
            return False, f"SHORT인데 디스카운트 ({current_price:.2f} < {mid_price:.2f})", mid_price, ote_low, ote_high
        if not (ote_high >= current_price >= ote_low):
            return False, f"SHORT인데 OTE 아님 ({current_price:.2f} ∉ {ote_high:.2f} ~ {ote_low:.2f})", mid_price, ote_low, ote_high
    else:
        return False, "방향 미지정", None, None, None

    return True, "pass", mid_price, ote_low, ote_high
