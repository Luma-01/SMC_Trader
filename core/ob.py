# core/ob.py
import pandas as pd
from notify.discord import send_discord_debug
from typing import List, Dict

def detect_ob(df: pd.DataFrame) -> List[Dict]:
    """
    정통 SMC 방식 Order Block 감지:
    - bullish: 하락 마감 음봉 뒤 상승 발생
    - bearish: 상승 마감 양봉 뒤 하락 발생
    """
    df = df.copy()
    ob_zones = []
    max_displacement_candles = 3
    for i in range(2, len(df) - max_displacement_candles):
        c1 = df.iloc[i - 2]
        c2 = df.iloc[i - 1]
        for j in range(1, max_displacement_candles + 1):
            if i + j >= len(df):
                break
            c_next = df.iloc[i + j]

            # Bearish OB: 상승 후 하락 displacement
            if c1['high'] < c2['high'] and c2['high'] > c_next['high'] and c_next['close'] < c_next['open']:
                ob_zones.append({
                    "type": "bearish",
                    "high": max(c2['open'], c2['close']),
                    "low": min(c2['open'], c2['close']),
                    "time": c2['time']
                })
                break

            # Bullish OB: 하락 후 상승 displacement
            if c1['low'] > c2['low'] and c2['low'] < c_next['low'] and c_next['close'] > c_next['open']:
                ob_zones.append({
                    "type": "bullish",
                    "high": max(c2['open'], c2['close']),
                    "low": min(c2['open'], c2['close']),
                    "time": c2['time']
                })
                break

    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    if ob_zones:
        last = ob_zones[-1]
        msg = f"[OB][{tf}] {symbol} - {len(ob_zones)}개 감지됨 | 최신: {last['type'].upper()} | {last['low']} ~ {last['high']}"
    else:
        msg = f"[OB][{tf}] {symbol} - 감지된 Order Block 없음"
    print(msg)
    #send_discord_debug(msg, "aggregated")

    return ob_zones
