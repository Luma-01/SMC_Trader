# core/protective.py

import pandas as pd
from typing import Optional, Dict, List


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


def get_improved_protective_level(
    ltf_df: pd.DataFrame,
    htf_df: Optional[pd.DataFrame],
    direction: str,
    entry_price: float,
    trigger_zone: Optional[Dict] = None,
    use_htf: bool = True
) -> Optional[Dict]:
    """
    개선된 보호선 산출 함수
    
    우선순위:
    1. 진입근거 존 기반 보호선 (OB/BB 상단/하단)
    2. HTF 구조적 보호선 (직전 고점/저점, 스윙 포인트)
    3. LTF 보호선 (스윙 포인트)
    4. 최후 폴백 (진입가 기준)
    
    Args:
        ltf_df: LTF DataFrame
        htf_df: HTF DataFrame (선택사항)
        direction: 'long' or 'short'
        entry_price: 진입가
        trigger_zone: 진입근거 존 정보
        use_htf: HTF 보호선 사용 여부
    
    Returns:
        Dict with 'protective_level', 'reason', 'priority' or None
    """
    try:
        protective_candidates = []
        
        # 1. 진입근거 존 기반 보호선 (최우선)
        if trigger_zone:
            if direction == "long":
                zone_low = trigger_zone.get('low')
                if zone_low and zone_low < entry_price:
                    protective_candidates.append({
                        'level': zone_low,
                        'reason': f"진입근거 존({trigger_zone.get('kind', 'zone')}) 하단",
                        'priority': 1,
                        'source': 'trigger_zone'
                    })
            else:  # short
                zone_high = trigger_zone.get('high')
                if zone_high and zone_high > entry_price:
                    protective_candidates.append({
                        'level': zone_high,
                        'reason': f"진입근거 존({trigger_zone.get('kind', 'zone')}) 상단",
                        'priority': 1,
                        'source': 'trigger_zone'
                    })
        
        # 2. HTF 구조적 보호선
        if use_htf and htf_df is not None and not htf_df.empty:
            htf_protective = get_htf_structural_protective(htf_df, direction, entry_price)
            if htf_protective:
                protective_candidates.append({
                    'level': htf_protective['protective_level'],
                    'reason': htf_protective['reason'],
                    'priority': 2,
                    'source': 'htf_structural'
                })
        
        # 3. LTF 보호선 (스윙 포인트)
        ltf_protective = get_protective_level(ltf_df, direction, lookback=30, span=2)
        if ltf_protective:
            # LTF 보호선이 진입가와 올바른 방향에 있는지 확인
            ltf_level = ltf_protective['protective_level']
            ltf_valid = (
                (direction == "long" and ltf_level < entry_price) or
                (direction == "short" and ltf_level > entry_price)
            )
            
            if ltf_valid:
                protective_candidates.append({
                    'level': ltf_level,
                    'reason': "LTF 스윙 포인트",
                    'priority': 3,
                    'source': 'ltf_swing'
                })
        
        # 4. 최적 보호선 선택
        if protective_candidates:
            # 우선순위 정렬
            protective_candidates.sort(key=lambda x: x['priority'])
            
            # 진입가와의 거리 및 방향 검증
            for candidate in protective_candidates:
                level = candidate['level']
                distance_ratio = abs(entry_price - level) / entry_price
                
                # 최소 거리 조건 (0.3% 이상)
                if distance_ratio >= 0.003:
                    return {
                        'protective_level': level,
                        'reason': candidate['reason'],
                        'priority': candidate['priority'],
                        'source': candidate['source'],
                        'distance_ratio': distance_ratio
                    }
        
        # 5. 최후 폴백: 진입가 기준 보호선
        if direction == "long":
            fallback_level = entry_price * 0.995  # 0.5% 아래
        else:
            fallback_level = entry_price * 1.005  # 0.5% 위
            
        return {
            'protective_level': fallback_level,
            'reason': "진입가 기준 폴백 (0.5%)",
            'priority': 99,
            'source': 'fallback',
            'distance_ratio': 0.005
        }
        
    except Exception as e:
        print(f"[IMPROVED_PROTECTIVE] 오류: {e}")
        return None


def get_htf_structural_protective(
    htf_df: pd.DataFrame,
    direction: str,
    entry_price: float,
    lookback: int = 20
) -> Optional[Dict]:
    """
    HTF 구조적 보호선 산출
    
    Args:
        htf_df: HTF DataFrame
        direction: 'long' or 'short'
        entry_price: 진입가
        lookback: 탐색 범위
    
    Returns:
        Dict with 'protective_level', 'reason' or None
    """
    if htf_df.empty or len(htf_df) < 3:
        return None
        
    try:
        recent_data = htf_df.tail(lookback)
        
        if direction == "long":
            # LONG: 최근 저점들 중 진입가보다 낮은 가장 높은 저점
            recent_lows = recent_data['low'].tolist()
            valid_lows = [low for low in recent_lows if low < entry_price]
            
            if valid_lows:
                structural_low = max(valid_lows)
                return {
                    'protective_level': structural_low,
                    'reason': f"HTF 구조적 저점({structural_low:.5f})"
                }
        else:  # short
            # SHORT: 최근 고점들 중 진입가보다 높은 가장 낮은 고점
            recent_highs = recent_data['high'].tolist()
            valid_highs = [high for high in recent_highs if high > entry_price]
            
            if valid_highs:
                structural_high = min(valid_highs)
                return {
                    'protective_level': structural_high,
                    'reason': f"HTF 구조적 고점({structural_high:.5f})"
                }
        
        # 스윙 포인트 기반 보호선 (보조)
        swing_protective = get_htf_swing_protective(recent_data, direction, entry_price)
        if swing_protective:
            return swing_protective
            
    except Exception as e:
        print(f"[HTF_STRUCTURAL_PROTECTIVE] 오류: {e}")
        
    return None


def get_htf_swing_protective(
    df: pd.DataFrame,
    direction: str,
    entry_price: float
) -> Optional[Dict]:
    """HTF 스윙 포인트 기반 보호선 산출"""
    if len(df) < 5:
        return None
        
    try:
        # 간단한 스윙 포인트 감지 (3봉 기준)
        highs = df['high'].tolist()
        lows = df['low'].tolist()
        
        swing_points = []
        
        for i in range(2, len(df) - 2):
            # 스윙 고점
            if highs[i] > highs[i-1] and highs[i] > highs[i+1] and highs[i] > highs[i-2] and highs[i] > highs[i+2]:
                swing_points.append({
                    'type': 'high',
                    'level': highs[i],
                    'index': i
                })
            
            # 스윙 저점
            if lows[i] < lows[i-1] and lows[i] < lows[i+1] and lows[i] < lows[i-2] and lows[i] < lows[i+2]:
                swing_points.append({
                    'type': 'low',
                    'level': lows[i],
                    'index': i
                })
        
        if not swing_points:
            return None
            
        # 최근 스윙 포인트 중 적절한 보호선 선택
        if direction == "long":
            # LONG: 최근 스윙 저점 중 진입가보다 낮은 것
            swing_lows = [sp for sp in swing_points if sp['type'] == 'low' and sp['level'] < entry_price]
            if swing_lows:
                latest_swing_low = max(swing_lows, key=lambda x: x['index'])
                return {
                    'protective_level': latest_swing_low['level'],
                    'reason': f"HTF 스윙 저점({latest_swing_low['level']:.5f})"
                }
        else:  # short
            # SHORT: 최근 스윙 고점 중 진입가보다 높은 것
            swing_highs = [sp for sp in swing_points if sp['type'] == 'high' and sp['level'] > entry_price]
            if swing_highs:
                latest_swing_high = max(swing_highs, key=lambda x: x['index'])
                return {
                    'protective_level': latest_swing_high['level'],
                    'reason': f"HTF 스윙 고점({latest_swing_high['level']:.5f})"
                }
                
    except Exception as e:
        print(f"[HTF_SWING_PROTECTIVE] 오류: {e}")
        
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
