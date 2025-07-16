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
            
            # 기관성 FVG 점수 계산
            avg_range = df["high"].iloc[max(0, i-10):i+1].sub(df["low"].iloc[max(0, i-10):i+1]).mean()
            institutional_score = 0
            
            # 큰 FVG 크기 (평균 범위의 50% 이상)
            if float(width) > avg_range * 0.5:
                institutional_score += 1
            
            # 볼륨 확인 (있을 때만)
            if 'volume' in df.columns:
                vol_avg = df['volume'].iloc[max(0, i-10):i+1].mean()
                if df['volume'].iloc[i-1] > vol_avg * 1.3:  # 높은 볼륨
                    institutional_score += 1
            
            fvg_zones.append({
                "type": "bullish",
                "low": float(low),
                "high": float(high),
                "time": df["time"].iloc[i],
                "institutional_score": institutional_score,
                "pattern": "fvg"
            })

        # 하락 FVG
        elif Decimal(str(c1['low'])) > Decimal(str(c3['high'])):
            low = Decimal(str(c3['high'])).quantize(tick_size)
            high = Decimal(str(c1['low'])).quantize(tick_size)
            width = high - low
            if width < min_width:
                continue
            
            # 기관성 FVG 점수 계산
            avg_range = df["high"].iloc[max(0, i-10):i+1].sub(df["low"].iloc[max(0, i-10):i+1]).mean()
            institutional_score = 0
            
            # 큰 FVG 크기 (평균 범위의 50% 이상)
            if float(width) > avg_range * 0.5:
                institutional_score += 1
            
            # 볼륨 확인 (있을 때만)
            if 'volume' in df.columns:
                vol_avg = df['volume'].iloc[max(0, i-10):i+1].mean()
                if df['volume'].iloc[i-1] > vol_avg * 1.3:  # 높은 볼륨
                    institutional_score += 1
            
            fvg_zones.append({
                "type": "bearish",
                "low": float(low),
                "high": float(high),
                "time": df["time"].iloc[i],
                "institutional_score": institutional_score,
                "pattern": "fvg"
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
