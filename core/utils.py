# core/utils.py (또는 적절한 위치)

import pandas as pd
from typing import Tuple, Optional, Dict
from config.settings import HTF_PREMIUM_DISCOUNT_WINDOW

def refined_premium_discount_filter(htf_df: pd.DataFrame, ltf_df: pd.DataFrame, direction: str, window: Optional[int] = None) -> Tuple[bool, str, float, float, float]:
    if htf_df.empty or ltf_df.empty or 'close' not in ltf_df.columns:
        return False, "데이터 부족", 0.0, 0.0, 0.0

    # window가 None이면 설정값 사용
    if window is None:
        window = HTF_PREMIUM_DISCOUNT_WINDOW

    htf_recent = htf_df.tail(window)
    htf_high = float(htf_recent['high'].max())
    htf_low = float(htf_recent['low'].min())
    range_size = htf_high - htf_low
    current_price = float(ltf_df['close'].dropna().iloc[-1])

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

    return True, f"프리미엄/디스카운트 필터 통과", mid_price, htf_low, htf_high


def get_htf_structural_invalidation(
    htf_df: pd.DataFrame,
    direction: str,
    entry_price: float,
    trigger_zone: Optional[Dict] = None,
    lookback: int = 20
) -> Optional[Dict]:
    """
    HTF 구조적 무효화 지점을 산출하는 함수
    
    Args:
        htf_df: HTF DataFrame
        direction: 'long' or 'short'
        entry_price: 진입가
        trigger_zone: 진입근거 존 정보 (OB, BB 등)
        lookback: 구조적 무효화 지점 탐색 범위
    
    Returns:
        Dict with 'invalidation_level', 'reason', 'time' or None
    """
    if htf_df.empty or len(htf_df) < 3:
        return None
        
    try:
        # 1. 진입근거 존 기반 무효화 지점 (최우선)
        if trigger_zone:
            if direction == "long":
                # LONG: OB/BB 하단 아래로 이탈 시 무효화
                zone_low = trigger_zone.get('low')
                if zone_low and zone_low < entry_price:
                    return {
                        'invalidation_level': zone_low,
                        'reason': f"진입근거 존({trigger_zone.get('kind', 'zone')}) 하단 이탈",
                        'time': trigger_zone.get('time'),
                        'priority': 1
                    }
            else:  # short
                # SHORT: OB/BB 상단 위로 이탈 시 무효화
                zone_high = trigger_zone.get('high')
                if zone_high and zone_high > entry_price:
                    return {
                        'invalidation_level': zone_high,
                        'reason': f"진입근거 존({trigger_zone.get('kind', 'zone')}) 상단 이탈",
                        'time': trigger_zone.get('time'),
                        'priority': 1
                    }
        
        # 2. HTF 구조적 극값 (직전 고점/저점) 기반 무효화
        recent_data = htf_df.tail(lookback)
        
        if direction == "long":
            # LONG: 최근 저점들 중 진입가보다 낮은 가장 높은 저점
            recent_lows = recent_data['low'].tolist()
            valid_lows = [low for low in recent_lows if low < entry_price]
            
            if valid_lows:
                structural_low = max(valid_lows)
                # 해당 저점의 시간 찾기
                low_matches = recent_data[recent_data['low'] == structural_low]
                low_time = low_matches['time'].iloc[-1] if not low_matches.empty else recent_data['time'].iloc[-1]
                
                return {
                    'invalidation_level': structural_low,
                    'reason': f"HTF 구조적 저점({structural_low:.5f}) 이탈",
                    'time': low_time,
                    'priority': 2
                }
        else:  # short
            # SHORT: 최근 고점들 중 진입가보다 높은 가장 낮은 고점
            recent_highs = recent_data['high'].tolist()
            valid_highs = [high for high in recent_highs if high > entry_price]
            
            if valid_highs:
                structural_high = min(valid_highs)
                # 해당 고점의 시간 찾기
                high_matches = recent_data[recent_data['high'] == structural_high]
                high_time = high_matches['time'].iloc[-1] if not high_matches.empty else recent_data['time'].iloc[-1]
                
                return {
                    'invalidation_level': structural_high,
                    'reason': f"HTF 구조적 고점({structural_high:.5f}) 이탈",
                    'time': high_time,
                    'priority': 2
                }
        
        # 3. 스윙 포인트 기반 무효화 (보조)
        swing_invalidation = get_swing_invalidation(recent_data, direction, entry_price)
        if swing_invalidation:
            swing_invalidation['priority'] = 3
            return swing_invalidation
            
        # 4. 최후 폴백: ATR 기반 무효화
        atr_invalidation = get_atr_based_invalidation(htf_df, direction, entry_price)
        if atr_invalidation:
            atr_invalidation['priority'] = 4
            return atr_invalidation
            
    except Exception as e:
        print(f"[HTF_INVALIDATION] 오류: {e}")
        
    return None


def get_swing_invalidation(df: pd.DataFrame, direction: str, entry_price: float) -> Optional[Dict]:
    """스윙 포인트 기반 무효화 지점 산출"""
    if len(df) < 5:
        return None
        
    try:
        # 간단한 스윙 포인트 감지 (3봉 기준)
        highs = df['high'].tolist()
        lows = df['low'].tolist()
        times = df['time'].tolist()
        
        swing_points = []
        
        for i in range(2, len(df) - 2):
            # 스윙 고점
            if highs[i] > highs[i-1] and highs[i] > highs[i+1] and highs[i] > highs[i-2] and highs[i] > highs[i+2]:
                swing_points.append({
                    'type': 'high',
                    'level': highs[i],
                    'time': times[i],
                    'index': i
                })
            
            # 스윙 저점
            if lows[i] < lows[i-1] and lows[i] < lows[i+1] and lows[i] < lows[i-2] and lows[i] < lows[i+2]:
                swing_points.append({
                    'type': 'low',
                    'level': lows[i],
                    'time': times[i],
                    'index': i
                })
        
        if not swing_points:
            return None
            
        # 최근 스윙 포인트 중 적절한 무효화 지점 선택
        if direction == "long":
            # LONG: 최근 스윙 저점 중 진입가보다 낮은 것
            swing_lows = [sp for sp in swing_points if sp['type'] == 'low' and sp['level'] < entry_price]
            if swing_lows:
                latest_swing_low = max(swing_lows, key=lambda x: x['index'])
                return {
                    'invalidation_level': latest_swing_low['level'],
                    'reason': f"HTF 스윙 저점({latest_swing_low['level']:.5f}) 이탈",
                    'time': latest_swing_low['time']
                }
        else:  # short
            # SHORT: 최근 스윙 고점 중 진입가보다 높은 것
            swing_highs = [sp for sp in swing_points if sp['type'] == 'high' and sp['level'] > entry_price]
            if swing_highs:
                latest_swing_high = max(swing_highs, key=lambda x: x['index'])
                return {
                    'invalidation_level': latest_swing_high['level'],
                    'reason': f"HTF 스윙 고점({latest_swing_high['level']:.5f}) 이탈",
                    'time': latest_swing_high['time']
                }
                
    except Exception as e:
        print(f"[SWING_INVALIDATION] 오류: {e}")
        
    return None


def get_atr_based_invalidation(df: pd.DataFrame, direction: str, entry_price: float) -> Optional[Dict]:
    """ATR 기반 무효화 지점 산출 (최후 폴백)"""
    if len(df) < 15:
        return None
        
    try:
        # ATR 계산
        df_calc = df.copy()
        df_calc['prev_close'] = df_calc['close'].shift(1)
        
        tr = pd.concat([
            df_calc['high'] - df_calc['low'],
            (df_calc['high'] - df_calc['prev_close']).abs(),
            (df_calc['low'] - df_calc['prev_close']).abs(),
        ], axis=1).max(axis=1)
        
        atr = tr.rolling(window=14).mean().iloc[-1]
        
        if pd.isna(atr):
            return None
            
        # ATR 기반 무효화 지점 (2 ATR 거리)
        atr_multiplier = 2.0
        
        if direction == "long":
            invalidation_level = entry_price - (atr * atr_multiplier)
        else:  # short
            invalidation_level = entry_price + (atr * atr_multiplier)
            
        return {
            'invalidation_level': invalidation_level,
            'reason': f"ATR 기반 무효화 ({atr_multiplier}x ATR = {atr:.5f})",
            'time': df['time'].iloc[-1]
        }
        
    except Exception as e:
        print(f"[ATR_INVALIDATION] 오류: {e}")
        
    return None


def calculate_improved_stop_loss(
    symbol: str,
    direction: str,
    entry_price: float,
    htf_df: pd.DataFrame,
    protective: Optional[float] = None,
    trigger_zone: Optional[Dict] = None,
    min_rr_base: float = 0.005
) -> Dict:
    """
    개선된 SL 산출 함수
    
    우선순위:
    1. 진입근거 존 기반 무효화 (OB/BB 상단/하단)
    2. HTF 구조적 무효화 (직전 고점/저점)
    3. Protective 레벨 (MSS 등)
    4. 최소 거리 폴백
    
    Returns:
        Dict with 'sl_level', 'reason', 'priority'
    """
    try:
        # 1. HTF 구조적 무효화 지점 산출
        htf_invalidation = get_htf_structural_invalidation(
            htf_df, direction, entry_price, trigger_zone
        )
        
        # 2. SL 후보들 수집
        sl_candidates = []
        
        # 2-1. HTF 구조적 무효화 (최우선)
        if htf_invalidation:
            sl_candidates.append({
                'level': htf_invalidation['invalidation_level'],
                'reason': htf_invalidation['reason'],
                'priority': htf_invalidation['priority']
            })
        
        # 2-2. Protective 레벨 (MSS 등)
        if protective is not None:
            # Protective가 진입가와 올바른 방향에 있는지 확인
            protective_valid = (
                (direction == "long" and protective < entry_price) or
                (direction == "short" and protective > entry_price)
            )
            
            if protective_valid:
                sl_candidates.append({
                    'level': protective,
                    'reason': "MSS/보호선 기반",
                    'priority': 5
                })
        
        # 3. 최적 SL 선택 (우선순위 및 거리 고려)
        if sl_candidates:
            # 우선순위 정렬
            sl_candidates.sort(key=lambda x: x['priority'])
            
            # 최소 거리 조건 확인
            for candidate in sl_candidates:
                sl_level = candidate['level']
                risk_ratio = abs(entry_price - sl_level) / entry_price
                
                # 최소 거리 조건 만족 시 선택
                if risk_ratio >= min_rr_base:
                    return {
                        'sl_level': sl_level,
                        'reason': candidate['reason'],
                        'priority': candidate['priority'],
                        'risk_ratio': risk_ratio
                    }
        
        # 4. 최후 폴백: 최소 거리 기반 SL
        if direction == "long":
            fallback_sl = entry_price * (1 - min_rr_base)
        else:  # short
            fallback_sl = entry_price * (1 + min_rr_base)
            
        return {
            'sl_level': fallback_sl,
            'reason': f"최소 거리 폴백 ({min_rr_base*100:.1f}%)",
            'priority': 99,
            'risk_ratio': min_rr_base
        }
        
    except Exception as e:
        print(f"[IMPROVED_SL] 오류: {e}")
        
        # 오류 시 기본 SL
        if direction == "long":
            emergency_sl = entry_price * 0.98
        else:
            emergency_sl = entry_price * 1.02
            
        return {
            'sl_level': emergency_sl,
            'reason': "오류 시 기본 SL (2%)",
            'priority': 999,
            'risk_ratio': 0.02
        }
