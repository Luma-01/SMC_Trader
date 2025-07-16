# core/utils.py (또는 적절한 위치)

import pandas as pd
from typing import Tuple

def refined_premium_discount_filter(htf_df: pd.DataFrame, ltf_df: pd.DataFrame, direction: str, window: int = 20) -> Tuple[bool, str, float, float, float]:
    if htf_df.empty or ltf_df.empty or 'close' not in ltf_df.columns:
        return False, "데이터 부족", None, None, None

    htf_recent = htf_df.tail(window)
    htf_high = htf_recent['high'].max()
    htf_low = htf_recent['low'].min()
    mid_price = (htf_high + htf_low) / 2
    current_price = ltf_df['close'].dropna().iloc[-1]

    if direction == 'long':
        if current_price > mid_price:
            return False, f"LONG인데 프리미엄 ({current_price:.2f} > {mid_price:.2f})", mid_price, htf_low, htf_high
    elif direction == 'short':
        if current_price < mid_price:
            return False, f"SHORT인데 디스카운트 ({current_price:.2f} < {mid_price:.2f})", mid_price, htf_low, htf_high
    else:
        return False, "방향 미지정", None, None, None

    return True, "pass", mid_price, htf_low, htf_high
