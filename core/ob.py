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
    for i in range(2, len(df)):
        c1 = df.iloc[i - 2]
        c2 = df.iloc[i - 1]
        c3 = df.iloc[i]

        # SMC식 기준 OB 생성: 임펄스 전 마지막 캔들
        # Bearish OB: 상승 후 하락 displacement
        if c1['high'] < c2['high'] and c2['high'] > c3['high'] and c3['close'] < c3['open']:
            ob_zones.append({
                "type": "bearish",
                "high": max(c2['open'], c2['close']),
                "low": min(c2['open'], c2['close']),
                "time": c2['time']
            })
        # Bullish OB: 하락 후 상승 displacement
        elif c1['low'] > c2['low'] and c2['low'] < c3['low'] and c3['close'] > c3['open']:
            ob_zones.append({
                "type": "bullish",
                "high": max(c2['open'], c2['close']),
                "low": min(c2['open'], c2['close']),
                "time": c2['time']
            })

    symbol = df.attrs.get("symbol", "UNKNOWN")
    if ob_zones:
        last = ob_zones[-1]
        msg = f"[OB] {symbol} - {len(ob_zones)}개 감지됨 | 최신: {last['type'].upper()} | {last['low']} ~ {last['high']}"
    else:
        msg = f"[OB] {symbol} - 감지된 Order Block 없음"
    print(msg)
    #send_discord_debug(msg, "aggregated")

    return ob_zones
