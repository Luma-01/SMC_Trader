# core/liquidity.py

import pandas as pd
from typing import List, Dict, Tuple
from decimal import Decimal
from notify.discord import send_discord_debug

def detect_equal_levels(df: pd.DataFrame, tolerance_pct: float = 0.1) -> List[Dict]:
    """
    Equal Highs/Lows 감지 - 유동성 레벨 식별
    
    Args:
        df: 캔들 데이터
        tolerance_pct: 허용 오차 비율 (기본 0.1%)
    
    Returns:
        List of liquidity levels
    """
    if len(df) < 3:
        return []
    
    liquidity_levels = []
    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    
    # Equal Highs 감지 (Buy Side Liquidity)
    for i in range(1, len(df) - 1):
        current_high = df['high'].iloc[i]
        
        # 앞뒤 캔들들과 비교
        matches = []
        for j in range(max(0, i - 10), min(len(df), i + 11)):
            if j == i:
                continue
                
            other_high = df['high'].iloc[j]
            if abs(current_high - other_high) / current_high < tolerance_pct / 100:
                matches.append(j)
        
        if len(matches) >= 1:  # 최소 1개 이상의 매칭
            liquidity_levels.append({
                "type": "buy_side_liquidity",
                "price": current_high,
                "time": df['time'].iloc[i],
                "matches": len(matches),
                "strength": min(3, len(matches))  # 최대 3점
            })
    
    # Equal Lows 감지 (Sell Side Liquidity)  
    for i in range(1, len(df) - 1):
        current_low = df['low'].iloc[i]
        
        # 앞뒤 캔들들과 비교
        matches = []
        for j in range(max(0, i - 10), min(len(df), i + 11)):
            if j == i:
                continue
                
            other_low = df['low'].iloc[j]
            if abs(current_low - other_low) / current_low < tolerance_pct / 100:
                matches.append(j)
        
        if len(matches) >= 1:  # 최소 1개 이상의 매칭
            liquidity_levels.append({
                "type": "sell_side_liquidity", 
                "price": current_low,
                "time": df['time'].iloc[i],
                "matches": len(matches),
                "strength": min(3, len(matches))  # 최대 3점
            })
    
    # 중복 제거 및 정렬
    liquidity_levels = remove_duplicate_levels(liquidity_levels, tolerance_pct)
    liquidity_levels.sort(key=lambda x: x['strength'], reverse=True)
    
    # 상위 10개만 유지
    liquidity_levels = liquidity_levels[:10]
    
    if liquidity_levels:
        print(f"[LIQUIDITY] {symbol} ({tf}) → {len(liquidity_levels)}개 유동성 레벨 감지")
        for level in liquidity_levels[:3]:  # 상위 3개만 로그
            print(f"  {level['type']}: {level['price']:.5f} (강도: {level['strength']})")
    
    return liquidity_levels

def remove_duplicate_levels(levels: List[Dict], tolerance_pct: float) -> List[Dict]:
    """
    중복되는 유동성 레벨 제거
    """
    if not levels:
        return []
    
    filtered_levels = []
    
    for level in levels:
        is_duplicate = False
        for existing in filtered_levels:
            if (level['type'] == existing['type'] and 
                abs(level['price'] - existing['price']) / level['price'] < tolerance_pct / 100):
                # 더 강한 레벨로 교체
                if level['strength'] > existing['strength']:
                    filtered_levels.remove(existing)
                    break
                else:
                    is_duplicate = True
                    break
        
        if not is_duplicate:
            filtered_levels.append(level)
    
    return filtered_levels

def is_liquidity_sweep(df: pd.DataFrame, liquidity_level: float, direction: str) -> bool:
    """
    유동성 사냥 감지
    
    Args:
        df: 최근 캔들 데이터
        liquidity_level: 유동성 레벨 가격
        direction: 'up' or 'down'
    
    Returns:
        bool: 유동성 사냥 발생 여부
    """
    if len(df) < 3:
        return False
    
    recent_candles = df.tail(5)
    
    if direction == 'up':
        # 상승 방향 유동성 사냥: 레벨 돌파 후 즉시 반전
        breakout_candles = recent_candles[recent_candles['high'] > liquidity_level]
        if len(breakout_candles) > 0:
            # 돌파 후 즉시 반전했는지 확인
            last_candle = recent_candles.iloc[-1]
            if last_candle['close'] < liquidity_level:
                return True
    else:
        # 하락 방향 유동성 사냥: 레벨 하향 돌파 후 즉시 반전
        breakout_candles = recent_candles[recent_candles['low'] < liquidity_level]
        if len(breakout_candles) > 0:
            # 돌파 후 즉시 반전했는지 확인
            last_candle = recent_candles.iloc[-1]
            if last_candle['close'] > liquidity_level:
                return True
    
    return False

def get_nearest_liquidity_level(levels: List[Dict], current_price: float, direction: str) -> Dict:
    """
    현재 가격에서 가장 가까운 유동성 레벨 찾기
    
    Args:
        levels: 유동성 레벨 리스트
        current_price: 현재 가격
        direction: 'long' or 'short'
    
    Returns:
        Dict: 가장 가까운 유동성 레벨
    """
    if not levels:
        return None
    
    if direction == 'long':
        # LONG 포지션: 위쪽 BSL 찾기
        relevant_levels = [l for l in levels if l['type'] == 'buy_side_liquidity' and l['price'] > current_price]
    else:
        # SHORT 포지션: 아래쪽 SSL 찾기
        relevant_levels = [l for l in levels if l['type'] == 'sell_side_liquidity' and l['price'] < current_price]
    
    if not relevant_levels:
        return None
    
    # 가장 가까운 레벨 찾기
    nearest_level = min(relevant_levels, key=lambda x: abs(x['price'] - current_price))
    return nearest_level