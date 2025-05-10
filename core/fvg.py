# core/fvg.py

import pandas as pd
from typing import List, Dict
from notify.discord import send_discord_debug

def detect_fvg(df: pd.DataFrame) -> List[Dict]:
    fvg_zones = []

    # 기본 tick size (추후 get_tick_size(symbol)로 교체 가능)
    tick_size = 0.0001
    min_width = tick_size * 3  # 최소 유효 폭 조건

    for i in range(2, len(df)):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]

        # 상승 FVG
        if c1['high'] < c3['low']:
            width = c3['low'] - c1['high']
            if width < min_width:
                continue
            fvg_zones.append({
                "type": "bullish",
                "low": c3['low'],
                "high": c1['high'],
                "time": df['time'].iloc[i]
            })

        # 하락 FVG
        elif c1['low'] > c3['high']:
            width = c1['low'] - c3['high']
            if width < min_width:
                continue
            fvg_zones.append({
                "type": "bearish",
                "low": c1['low'],
                "high": c3['high'],
                "time": df['time'].iloc[i]
            })

    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    count = len(fvg_zones)
    print(f"📉 [FVG][{tf}] {symbol} - FVG {count}개 감지됨")
    #send_discord_debug(f"📉 [FVG] {symbol} - FVG {count}개 감지됨", "aggregated")
    return fvg_zones
