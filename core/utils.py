# core/utils.py (또는 적절한 위치)

import pandas as pd
from typing import Tuple
from config.settings import HTF_PREMIUM_DISCOUNT_WINDOW

def refined_premium_discount_filter(htf_df: pd.DataFrame, ltf_df: pd.DataFrame, direction: str, window: int = None) -> Tuple[bool, str, float, float, float]:
    if htf_df.empty or ltf_df.empty or 'close' not in ltf_df.columns:
        return False, "데이터 부족", None, None, None

    # window가 None이면 설정값 사용
    if window is None:
        window = HTF_PREMIUM_DISCOUNT_WINDOW

    htf_recent = htf_df.tail(window)
    htf_high = htf_recent['high'].max()
    htf_low = htf_recent['low'].min()
    range_size = htf_high - htf_low
    current_price = ltf_df['close'].dropna().iloc[-1]

    # 동적 Premium/Discount 존 설정 (30-70% 대신 50% 고정)
    premium_threshold = htf_high - (range_size * 0.3)  # 상위 30%
    discount_threshold = htf_low + (range_size * 0.3)   # 하위 30%
    mid_price = (htf_high + htf_low) / 2

    if direction == 'long':
        # LONG은 discount 존에서만 진입 허용
        if current_price > premium_threshold:
            return False, f"LONG인데 프리미엄 존 ({current_price:.5f} > {premium_threshold:.5f})", mid_price, htf_low, htf_high
    elif direction == 'short':
        # SHORT는 premium 존에서만 진입 허용
        if current_price < discount_threshold:
            return False, f"SHORT인데 디스카운트 존 ({current_price:.5f} < {discount_threshold:.5f})", mid_price, htf_low, htf_high
    else:
        return False, "방향 미지정", None, None, None

    return True, "pass", mid_price, htf_low, htf_high
