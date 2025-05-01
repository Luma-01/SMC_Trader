import pandas as pd
from core.structure import detect_structure
from core.fvg import detect_fvg

def is_iof_entry(htf_df: pd.DataFrame, ltf_df: pd.DataFrame) -> (bool, str):
    # 1. HTF 구조 판단
    htf_struct = detect_structure(htf_df)
    recent = htf_struct['structure'].dropna().iloc[-1] if not htf_struct['structure'].dropna().empty else None

    direction = None
    if recent == 'BOS_up':
        direction = 'long'
    elif recent == 'BOS_down':
        direction = 'short'
    else:
        return False, None

    # 2. Premium / Discount 필터
    htf_high = htf_df['high'].max()
    htf_low = htf_df['low'].min()
    mid_price = (htf_high + htf_low) / 2
    current_price = ltf_df['close'].iloc[-1]

    if direction == 'long' and current_price > mid_price:
        return False, None
    if direction == 'short' and current_price < mid_price:
        return False, None

    # 3. FVG 진입 여부
    fvg_zones = detect_fvg(ltf_df)
    if not fvg_zones:
        return False, None

    latest_fvg = fvg_zones[-1]
    if (
        direction == 'long' and latest_fvg['type'] == 'bullish'
        and latest_fvg['low'] <= current_price <= latest_fvg['high']
    ):
        return True, direction

    elif (
        direction == 'short' and latest_fvg['type'] == 'bearish'
        and latest_fvg['low'] <= current_price <= latest_fvg['high']
    ):
        return True, direction

    return False, None
