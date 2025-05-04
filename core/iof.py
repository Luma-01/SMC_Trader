# core/iof.py

import pandas as pd
from core.structure import detect_structure
from core.fvg import detect_fvg
from notify.discord import send_discord_debug
from typing import Tuple

def is_iof_entry(htf_df: pd.DataFrame, ltf_df: pd.DataFrame) -> Tuple[bool, str]:
    # 1. HTF 구조 판단
    htf_struct = detect_structure(htf_df)
    if htf_struct is None or not isinstance(htf_struct, pd.DataFrame) or 'structure' not in htf_struct.columns:
        send_discord_debug("[IOF] ❌ detect_structure() 반환 오류 → 진입 판단 불가", "aggregated")
        return False, None
    if not isinstance(htf_struct, pd.DataFrame) or 'structure' not in htf_struct.columns:
        print("[IOF] ❌ detect_structure() 반환 오류 → 진입 판단 불가")
        send_discord_debug("[IOF] ❌ detect_structure() 반환 오류 → 진입 판단 불가", "aggregated")
        return False, None
    structure_series = htf_struct['structure'].dropna()
    if structure_series.empty:
        print("[IOF] ❌ 구조 데이터 없음 → 진입 판단 불가")
        send_discord_debug("[IOF] ❌ 구조 데이터 없음 → 진입 판단 불가", "aggregated")
        return False, None
    recent = structure_series.iloc[-1]

    direction = None
    if recent == 'BOS_up':
        direction = 'long'
    elif recent == 'BOS_down':
        direction = 'short'
    else:
        send_discord_debug("[IOF] ❌ BOS 미충족 → 진입 불가", "aggregated")
        return False, None

    # 2. Premium / Discount 필터
    htf_high = htf_df['high'].max()
    htf_low = htf_df['low'].min()
    mid_price = (htf_high + htf_low) / 2
    if ltf_df.empty or 'close' not in ltf_df.columns or ltf_df['close'].dropna().empty:
        send_discord_debug("[IOF] ❌ LTF 데이터 부족 → 진입 판단 불가", "aggregated")
        return False, None
    current_price = ltf_df['close'].dropna().iloc[-1]
    if direction == 'long' and current_price > mid_price:
        send_discord_debug(f"[IOF] ❌ LONG인데 가격이 프리미엄 영역 ({current_price:.2f} > {mid_price:.2f})", "aggregated")
        return False, None
    if direction == 'short' and current_price < mid_price:
        send_discord_debug(f"[IOF] ❌ SHORT인데 가격이 디스카운트 영역 ({current_price:.2f} < {mid_price:.2f})", "aggregated")
        return False, None

    # 3. FVG 진입 여부
    fvg_zones = detect_fvg(ltf_df)
    if not fvg_zones:
        send_discord_debug("[IOF] ❌ FVG 감지 안됨", "aggregated")
        return False, None

    latest_fvg = fvg_zones[-1]
    if (
        direction == 'long' and latest_fvg['type'] == 'bullish'
        and latest_fvg['low'] <= current_price <= latest_fvg['high']
    ):
        send_discord_debug(f"[IOF] LONG 진입 조건 충족 | 가격: {current_price}", "aggregated")
        return True, direction

    elif (
        direction == 'short' and latest_fvg['type'] == 'bearish'
        and latest_fvg['low'] <= current_price <= latest_fvg['high']
    ):
        send_discord_debug(f"[IOF] SHORT 진입 조건 충족 | 가격: {current_price}", "aggregated")
        return True, direction

    send_discord_debug("[IOF] ❌ FVG 영역 내 진입 아님", "aggregated")
    return False, None
