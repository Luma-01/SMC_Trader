# core/fvg.py

import pandas as pd
from typing import List, Dict
from notify.discord import send_discord_debug
from decimal import Decimal, ROUND_DOWN

def detect_fvg(df: pd.DataFrame) -> List[Dict]:
    fvg_zones = []

    # 기본 tick size (추후 get_tick_size(symbol)로 교체 가능)
    tick_size = Decimal("0.0001")
    min_width = tick_size * 3  # 최소 유효 폭 조건

    for i in range(2, len(df)):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]

        # 상승 FVG
        if Decimal(str(c1['high'])) < Decimal(str(c3['low'])):
            low = Decimal(str(c1['high'])).quantize(tick_size)
            high = Decimal(str(c3['low'])).quantize(tick_size)
            width = high - low
            if width < min_width:
                continue
            fvg_zones.append({
                "type": "bullish",
                "low": str(low),
                "high": str(high),
                "time": df['time'].iloc[i]
            })

        # 하락 FVG
        elif Decimal(str(c1['low'])) > Decimal(str(c3['high'])):
            low = Decimal(str(c3['high'])).quantize(tick_size)
            high = Decimal(str(c1['low'])).quantize(tick_size)
            width = high - low
            if width < min_width:
                continue
            fvg_zones.append({
                "type": "bearish",
                "low": str(low),
                "high": str(high),
                "time": df['time'].iloc[i]
            })

    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    count = len(fvg_zones)
    print(f"📉 [FVG][{tf}] {symbol} - FVG {count}개 감지됨")
    #send_discord_debug(f"📉 [FVG] {symbol} - FVG {count}개 감지됨", "aggregated")
    return fvg_zones
