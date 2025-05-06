# core/iof.py

import pandas as pd
from core.structure import detect_structure
from core.fvg import detect_fvg
from core.ob import detect_ob
from core.bb import detect_bb
from core.utils import refined_premium_discount_filter
from notify.discord import send_discord_debug
from typing import Tuple

def is_iof_entry(htf_df: pd.DataFrame, ltf_df: pd.DataFrame) -> Tuple[bool, str]:
    # 1. HTF 구조 판단
    htf_struct = detect_structure(htf_df)
    if htf_struct is None or not isinstance(htf_struct, pd.DataFrame) or 'structure' not in htf_struct.columns:
        print("[IOF] ❌ detect_structure() 반환 오류 → 진입 판단 불가")
        #send_discord_debug("[IOF] ❌ detect_structure() 반환 오류 → 진입 판단 불가", "aggregated")
        return False, None
    if not isinstance(htf_struct, pd.DataFrame) or 'structure' not in htf_struct.columns:
        print("[IOF] ❌ detect_structure() 반환 오류 → 진입 판단 불가")
        #send_discord_debug("[IOF] ❌ detect_structure() 반환 오류 → 진입 판단 불가", "aggregated")
        return False, None
    structure_series = htf_struct['structure'].dropna()
    if structure_series.empty:
        print("[IOF] ❌ 구조 데이터 없음 → 진입 판단 불가")
        #send_discord_debug("[IOF] ❌ 구조 데이터 없음 → 진입 판단 불가", "aggregated")
        return False, None
    
    recent = structure_series.iloc[-1]
    if recent in ['BOS_up', 'CHoCH_up']:
        direction = 'long'
    elif recent in ['BOS_down', 'CHoCH_down']:
        direction = 'short'
    else:
        print(f"[IOF] ❌ 최근 구조 신호 미충족 → 최근 구조: {recent}")
        return False, None

    # 2. Premium / Discount 필터
    passed, reason, mid, ote_l, ote_h = refined_premium_discount_filter(htf_df, ltf_df, direction)
    if not passed:
        print(f"[IOF] ❌ {reason}")
        return False, None

    # 3. FVG 진입 여부
    fvg_zones = detect_fvg(ltf_df)
    if fvg_zones:
        for fvg in reversed(fvg_zones):
            if direction == 'long' and fvg['type'] == 'bullish':
                if fvg['low'] <= current_price <= fvg['high']:
                    print(f"[IOF] ✅ LONG 진입 조건 충족 | FVG 범위: {fvg['low']} ~ {fvg['high']} | 현재가: {current_price}")
                    send_discord_debug(f"[IOF] ✅ LONG 진입 조건 충족 | FVG 범위: {fvg['low']} ~ {fvg['high']} | 현재가: {current_price}", "aggregated")
                    return True, direction
            elif direction == 'short' and fvg['type'] == 'bearish':
                if fvg['low'] <= current_price <= fvg['high']:
                    print(f"[IOF] ✅ SHORT 진입 조건 충족 | FVG 범위: {fvg['low']} ~ {fvg['high']} | 현재가: {current_price}")
                    send_discord_debug(f"[IOF] ✅ SHORT 진입 조건 충족 | FVG 범위: {fvg['low']} ~ {fvg['high']} | 현재가: {current_price}", "aggregated")                 
                    return True, direction
    else:
        print("[IOF] ❌ FVG 감지 안됨")
        send_discord_debug("[IOF] ❌ FVG 감지 안됨", "aggregated")


    # 4. OB 진입 여부
    from core.ob import detect_ob
    ob_zones = detect_ob(ltf_df)
    if ob_zones:
        for ob in reversed(ob_zones):
            if ob['type'] == direction and ob['low'] <= current_price <= ob['high']:
                print(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (OB 기반) | OB 범위: {ob['low']} ~ {ob['high']} | 현재가: {current_price}")
                send_discord_debug(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (OB 기반) | OB 범위: {ob['low']} ~ {ob['high']} | 현재가: {current_price}", "aggregated")
                return True, direction
            
    else:
        print("[IOF] ❌ OB 감지 안됨")
        send_discord_debug("[IOF] ❌ OB 감지 안됨", "aggregated")            

    # 5. BB 진입 여부
    from core.bb import detect_bb
    bb_zones = detect_bb(ltf_df, ob_zones)
    if bb_zones:
        for bb in reversed(bb_zones):
            if bb['type'] == direction and bb['low'] <= current_price <= bb['high']:
                print(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (BB 기반) | BB 범위: {bb['low']} ~ {bb['high']} | 현재가: {current_price}")
                send_discord_debug(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (BB 기반) | BB 범위: {bb['low']} ~ {bb['high']} | 현재가: {current_price}", "aggregated")
                return True, direction
            
    else:
        print("[IOF] ❌ BB 감지 안됨")
        send_discord_debug("[IOF] ❌ BB 감지 안됨", "aggregated")            
            
    print(f"[IOF] ❌ FVG/OB/BB 영역 내 진입 아님 → 현재가: {current_price}")
    #send_discord_debug(f"[IOF] ❌ FVG/OB/BB 영역 내 진입 아님 → 현재가: {current_price}", "aggregated")
    return False, None