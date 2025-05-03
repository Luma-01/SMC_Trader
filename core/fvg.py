# core/fvg.py

import pandas as pd
from typing import List, Dict

def detect_fvg(df: pd.DataFrame) -> List[Dict]:
    fvg_zones = []

    for i in range(2, len(df)):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]

        # 상승 FVG
        if c1['high'] < c3['low']:
            fvg_zones.append({
                "type": "bullish",
                "low": c3['low'],
                "high": c1['high'],
                "time": df['time'].iloc[i]
            })

        # 하락 FVG
        elif c1['low'] > c3['high']:
            fvg_zones.append({
                "type": "bearish",
                "low": c1['low'],
                "high": c3['high'],
                "time": df['time'].iloc[i]
            })

    return fvg_zones
