# core/iof.py

import pandas as pd
from core.structure import detect_structure
from core.ob import detect_ob
from core.bb import detect_bb
from core.utils import refined_premium_discount_filter
from notify.discord import send_discord_debug
from typing import Tuple, Optional, Dict
from decimal import Decimal

#   True/False , 'long'|'short'|None ,  존 dict 또는 None
def is_iof_entry(
        htf_df: pd.DataFrame,
        ltf_df: pd.DataFrame,
        tick_size: Decimal
) -> Tuple[bool, Optional[str], Optional[Dict]]:
    trigger_zone = None        # ← 돌려줄 존 정보
    symbol = htf_df.attrs.get("symbol", "UNKNOWN")
    tf = htf_df.attrs.get("tf", "?")
    
    # 1. HTF 구조 판단
    htf_struct = detect_structure(htf_df)
    if htf_struct is None or not isinstance(htf_struct, pd.DataFrame) or 'structure' not in htf_struct.columns:
        print(f"[IOF] [{symbol}-{tf}] ❌ detect_structure() 반환 오류 → 진입 판단 불가")
        return False, None, None
    structure_series = htf_struct['structure'].dropna()
    if structure_series.empty:
        print(f"[IOF] [{symbol}-{tf}] ❌ 구조 데이터 없음 → 진입 판단 불가")
        return False, None, None
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
        direction = 'long'
    elif recent in ['BOS_down', 'CHoCH_down']:
        direction = 'short'
    else:
        print(f"[IOF] [{symbol}-{tf}] ❌ 최근 구조 신호 미충족 → 최근 구조: {recent}")
        return False, None, None

    if bias in ['LONG', 'SHORT']:
        if bias.lower() == direction:
            print(f"[IOF] [{symbol}-{tf}] ✅ Bias와 진입 방향 일치 → Bias={bias}, Direction={direction}")
            #send_discord_debug(f"[IOF] ✅ Bias와 진입 방향 일치 → Bias={bias}, Direction={direction}", "aggregated")
        else:
            print(f"[IOF] [{symbol}-{tf}] ⚠️ Bias와 진입 방향 불일치 → Bias={bias}, Direction={direction}")
            #send_discord_debug(f"[IOF] ⚠️ Bias와 진입 방향 불일치 → Bias={bias}, Direction={direction}", "aggregated")

    # 2. Premium / Discount 필터
    #passed, reason, mid, ote_l, ote_h = refined_premium_discount_filter(htf_df, ltf_df, direction)
    #if not passed:
        #print(f"[IOF] ❌ {reason}")
        #return False, direction, None

    # current_price 직접 정의 (PD ZONE 비활 임시 테스트용)
    if ltf_df.empty or 'close' not in ltf_df.columns or ltf_df['close'].dropna().empty:
        print("[IOF] ❌ LTF 종가 없음")
        return False, direction, None

    current_price = Decimal(str((ltf_df['high'].iloc[-1] + ltf_df['low'].iloc[-1]) / 2))
    current_price = Decimal(str(current_price)).quantize(tick_size)

    buffer = tick_size * 10  # ✅ 진입 완화용 버퍼 설정
    near_buffer = tick_size * 10  # ✅ 근접 로그용 완화 조건

    # 3. ⚠️ FVG 무시 → 스킵 (노이즈 감소)
    #    => detect_fvg() 호출/로그 삭제
    # ------------------------------------------------


    # 3. OB 진입 여부
    ob_zones = detect_ob(ltf_df)
    if ob_zones:
        for ob in reversed(ob_zones[-10:]):
            if ob['type'].lower() == direction:
                low = Decimal(str(ob['low'])).quantize(tick_size)
                high = Decimal(str(ob['high'])).quantize(tick_size)
                entry_low = (low - buffer).quantize(tick_size)
                entry_high = (high + buffer).quantize(tick_size)
                near_low = (low - near_buffer).quantize(tick_size)
                near_high = (high + near_buffer).quantize(tick_size)
                #print(f"[DEBUG] OB {ob['type']} ZONE: {low} ~ {high}, CURRENT: {current_price}")
                if near_low <= current_price <= near_high:
                    print(f"[NEAR MISS] 🔍 OB {ob['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}")
                    send_discord_debug(f"[NEAR MISS] OB {ob['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}", "aggregated")
                if entry_low <= current_price <= entry_high:
                    print(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (OB 기반) | OB 범위: {ob['low']} ~ {ob['high']} | 현재가: {current_price}")
                    send_discord_debug(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (OB 기반) | OB 범위: {ob['low']} ~ {ob['high']} | 현재가: {current_price}", "aggregated")
                    
                    trigger_zone = {
                        "kind": "ob",
                        "type": ob["type"],
                        "low":  float(low),
                        "high": float(high)
                    }
                    return True, direction, trigger_zone
            
    else:
        print("[IOF] ❌ OB 감지 안됨")
        send_discord_debug("[IOF] ❌ OB 감지 안됨", "aggregated")            

    # 4. BB 진입 여부
    bb_zones = detect_bb(ltf_df, ob_zones)
    if bb_zones:
        for bb in reversed(bb_zones[-10:]):
            if bb['type'].lower() == direction:
                low = Decimal(str(bb['low'])).quantize(tick_size)
                high = Decimal(str(bb['high'])).quantize(tick_size)
                entry_low = (low - buffer).quantize(tick_size)
                entry_high = (high + buffer).quantize(tick_size)
                near_low = (low - near_buffer).quantize(tick_size)
                near_high = (high + near_buffer).quantize(tick_size)
                #print(f"[DEBUG] BB {bb['type']} ZONE: {low} ~ {high}, CURRENT: {current_price}")
                if near_low <= current_price <= near_high:
                    print(f"[NEAR MISS] 🔍 BB {bb['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}")
                    send_discord_debug(f"[NEAR MISS] BB {bb['type']} 근접 | 범위: {low} ~ {high} | 현재가: {current_price}", "aggregated")
                if entry_low <= current_price <= entry_high:
                    print(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (BB 기반) | BB 범위: {bb['low']} ~ {bb['high']} | 현재가: {current_price}")
                    send_discord_debug(f"[IOF] ✅ {direction.upper()} 진입 조건 충족 (BB 기반) | BB 범위: {bb['low']} ~ {bb['high']} | 현재가: {current_price}", "aggregated")

                    trigger_zone = {
                        "kind": "bb",
                        "type": bb["type"],
                        "low":  float(low),
                        "high": float(high)
                    }
                    return True, direction, trigger_zone
            
    else:
        print("[IOF] ❌ BB 감지 안됨")
        send_discord_debug("[IOF] ❌ BB 감지 안됨", "aggregated")            
            
    print(f"[IOF] [{symbol}-{tf}] ❌ OB/BB 영역 내 진입 아님 → 현재가: {current_price}")
    #send_discord_debug(f"[IOF] ❌ OB/BB 영역 내 진입 아님 → 현재가: {current_price}", "aggregated")
    return False, direction, None