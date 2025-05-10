# core/iof.py

import pandas as pd
from core.structure import detect_structure
from core.fvg import detect_fvg
from core.ob import detect_ob
from core.bb import detect_bb
from core.utils import refined_premium_discount_filter
from notify.discord import send_discord_debug
from typing import Tuple
from decimal import Decimal

def is_iof_entry(htf_df: pd.DataFrame, ltf_df: pd.DataFrame, tick_size: Decimal) -> Tuple[bool, str]:
    symbol = htf_df.attrs.get("symbol", "UNKNOWN")
    tf = htf_df.attrs.get("tf", "?")
    
    # 1. HTF 구조 판단
    htf_struct = detect_structure(htf_df)
    if htf_struct is None or not isinstance(htf_struct, pd.DataFrame) or 'structure' not in htf_struct.columns:
        print(f"[IOF] [{symbol}-{tf}] ❌ detect_structure() 반환 오류 → 진입 판단 불가")
        return False, None
    structure_series = htf_struct['structure'].dropna()
    if structure_series.empty:
        print(f"[IOF] [{symbol}-{tf}] ❌ 구조 데이터 없음 → 진입 판단 불가")
        return False, None
    recent = structure_series.iloc[-1]

    # Bias 판단 (기준: 구조의 마지막 값)
    bias = None
    if recent == 'BOS_up':
        bias = 'LONG'
    elif recent == 'BOS_down':
        bias = 'SHORT'
    elif recent.startswith('CHoCH'):
        bias = 'NONE'
    print(f"[BIAS] [{symbol}-{tf}] HTF 구조 기준 Bias = {bias} (최근 구조: {recent})")
    #send_discord_debug(f"[BIAS] HTF 구조 기준 Bias = {bias} (최근 구조: {recent})", "aggregated")

    if recent in ['BOS_up', 'CHoCH_up']:
        direction = 'LONG'
    elif recent in ['BOS_down', 'CHoCH_down']:
        direction = 'SHORT'
    else:
        print(f"[IOF] [{symbol}-{tf}] ❌ 최근 구조 신호 미충족 → 최근 구조: {recent}")
        return False, None

    if bias in ['LONG', 'SHORT']:
        if bias == direction:
            print(f"[IOF] [{symbol}-{tf}] ✅ Bias와 진입 방향 일치 → Bias={bias}, Direction={direction}")
            #send_discord_debug(f"[IOF] ✅ Bias와 진입 방향 일치 → Bias={bias}, Direction={direction}", "aggregated")
        else:
            print(f"[IOF] [{symbol}-{tf}] ⚠️ Bias와 진입 방향 불일치 → Bias={bias}, Direction={direction}")
            #send_discord_debug(f"[IOF] ⚠️ Bias와 진입 방향 불일치 → Bias={bias}, Direction={direction}", "aggregated")

    # 2. Premium / Discount 필터
    #passed, reason, mid, ote_l, ote_h = refined_premium_discount_filter(htf_df, ltf_df, direction)
    #if not passed:
        #print(f"[IOF] ❌ {reason}")
        #return False, direction

    # current_price 직접 정의 (PD ZONE 비활 임시 테스트용)
    if ltf_df.empty or 'close' not in ltf_df.columns or ltf_df['close'].dropna().empty:
        print("[IOF] ❌ LTF 종가 없음")
        return False, direction
    current_price = Decimal(str(ltf_df['close'].dropna().iloc[-1])).quantize(tick_size)

    buffer = tick_size * 2  # ✅ 진입 완화용 버퍼 설정
    near_buffer = tick_size * 4  # ✅ 근접 로그용 완화 조건 (예: ±4틱)

    # 3. FVG 진입 여부
    fvg_zones = detect_fvg(ltf_df)
    if fvg_zones:
        for fvg in reversed(fvg_zones[-3:]):
            low = Decimal(str(fvg['low'])).quantize(tick_size)
            high = Decimal(str(fvg['high'])).quantize(tick_size)
            #print(f"[DEBUG] FVG {fvg['type']} ZONE: {low} ~ {high}, CURRENT: {current_price}")
            if low - near_buffer <= current_price <= high + near_buffer:
                print(f"[NEAR MISS] FVG {fvg['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}")
                send_discord_debug(f"[NEAR MISS] FVG {fvg['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}", "aggregated")
            if direction == 'long' and fvg['type'] == 'bullish':
                if (low - buffer) <= current_price <= (high + buffer):
                    print(f"[IOF] ✅ LONG 진입 조건 충족 | FVG 범위: {fvg['low']} ~ {fvg['high']} | 현재가: {current_price}")
                    send_discord_debug(f"[IOF] ✅ LONG 진입 조건 충족 | FVG 범위: {fvg['low']} ~ {fvg['high']} | 현재가: {current_price}", "aggregated")
                    return True, direction
            elif direction == 'short' and fvg['type'] == 'bearish':
                if (low - buffer) <= current_price <= (high + buffer):
                    print(f"[IOF] ✅ SHORT 진입 조건 충족 | FVG 범위: {fvg['low']} ~ {fvg['high']} | 현재가: {current_price}")
                    send_discord_debug(f"[IOF] ✅ SHORT 진입 조건 충족 | FVG 범위: {fvg['low']} ~ {fvg['high']} | 현재가: {current_price}", "aggregated")                 
                    return True, direction
    else:
        print("[IOF] ❌ FVG 감지 안됨")
        send_discord_debug("[IOF] ❌ FVG 감지 안됨", "aggregated")


    # 4. OB 진입 여부
    ob_zones = detect_ob(ltf_df)
    if ob_zones:
        for ob in reversed(ob_zones[-3:]):
            if ob['type'] == direction:
                low = Decimal(str(ob['low'])).quantize(tick_size)
                high = Decimal(str(ob['high'])).quantize(tick_size)
                print(f"[DEBUG] OB {ob['type']} ZONE: {low} ~ {high}, CURRENT: {current_price}")
                if low - near_buffer <= current_price <= high + near_buffer:
                    print(f"[NEAR MISS] OB {ob['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}")
                    send_discord_debug(f"[NEAR MISS] OB {ob['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}", "aggregated")
                if (low - buffer) <= current_price <= (high + buffer):
                    print(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (OB 기반) | OB 범위: {ob['low']} ~ {ob['high']} | 현재가: {current_price}")
                    send_discord_debug(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (OB 기반) | OB 범위: {ob['low']} ~ {ob['high']} | 현재가: {current_price}", "aggregated")
                    return True, direction
            
    else:
        print("[IOF] ❌ OB 감지 안됨")
        send_discord_debug("[IOF] ❌ OB 감지 안됨", "aggregated")            

    # 5. BB 진입 여부
    bb_zones = detect_bb(ltf_df, ob_zones)
    if bb_zones:
        for bb in reversed(bb_zones[-3:]):
            if bb['type'] == direction:
                low = Decimal(str(bb['low'])).quantize(tick_size)
                high = Decimal(str(bb['high'])).quantize(tick_size)
                print(f"[DEBUG] BB {bb['type']} ZONE: {low} ~ {high}, CURRENT: {current_price}")
                if low - near_buffer <= current_price <= high + near_buffer:
                    print(f"[NEAR MISS] BB {bb['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}")
                    send_discord_debug(f"[NEAR MISS] BB {bb['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}", "aggregated")
                if (low - buffer) <= current_price <= (high + buffer):
                    print(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (BB 기반) | BB 범위: {bb['low']} ~ {bb['high']} | 현재가: {current_price}")
                    send_discord_debug(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (BB 기반) | BB 범위: {bb['low']} ~ {bb['high']} | 현재가: {current_price}", "aggregated")
                    return True, direction
            
    else:
        print("[IOF] ❌ BB 감지 안됨")
        send_discord_debug("[IOF] ❌ BB 감지 안됨", "aggregated")            
            
    print(f"[IOF] [{symbol}-{tf}] ❌ FVG/OB/BB 영역 내 진입 아님 → 현재가: {current_price}")
    #send_discord_debug(f"[IOF] ❌ FVG/OB/BB 영역 내 진입 아님 → 현재가: {current_price}", "aggregated")
    return False, direction