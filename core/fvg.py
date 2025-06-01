# core/fvg.py

import pandas as pd
from typing import List, Dict
from notify.discord import send_discord_debug
from decimal import Decimal, ROUND_DOWN

def detect_fvg(df: pd.DataFrame) -> List[Dict]:
    fvg_zones = []

    # tick_size 는 df.attrs 로부터 우선 시도 → 없으면 기본 0.0001
    tick_size = Decimal(
        str(df.attrs.get("tick_size", "0.0001"))
    ).normalize()
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
                "low": float(low),
                "high": float(high),
                "time": df["time"].iloc[i]
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
                "low": float(low),
                "high": float(high),
                "time": df["time"].iloc[i]
            })

    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    if fvg_zones:
        last = fvg_zones[-1]
        print(f"[FVG][{tf}] {symbol} → {last['type'].upper()} {last['low']}~{last['high']} (총 {len(fvg_zones)})")
    else:
        print(f"[FVG][{tf}] {symbol} → 감지 없음")
    #send_discord_debug(f"📉 [FVG] {symbol} - FVG {count}개 감지됨", "aggregated")
    return fvg_zones
