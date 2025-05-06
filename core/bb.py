# core/bb.py
import pandas as pd
from typing import List, Dict
from notify.discord import send_discord_debug

def detect_bb(df: pd.DataFrame, ob_zones: List[Dict]) -> List[Dict]:
    """
    정통 SMC 방식 Breaker Block 감지:
    - bullish BB: 이전 bullish OB 무효화 후 반등
    - bearish BB: 이전 bearish OB 무효화 후 반락
    """
    df = df.copy()
    bb_zones = []

    for ob in ob_zones:
        ob_type = ob['type']
        ob_high = ob['high']
        ob_low = ob['low']
        ob_time = ob['time']
        df_after = df[df['time'] > ob_time]

        for _, row in df_after.iterrows():
            # OB 무효화 → 반전 → BB 형성
            if ob_type == "bullish" and row['low'] < ob_low:
                bb_zones.append({
                    "type": "bearish",
                    "high": row['high'],
                    "low": row['low'],
                    "time": row['time']
                })
                break
            elif ob_type == "bearish" and row['high'] > ob_high:
                bb_zones.append({
                    "type": "bullish",
                    "high": row['high'],
                    "low": row['low'],
                    "time": row['time']
                })
                break

    symbol = df.attrs.get("symbol", "UNKNOWN")
    print(f"[BB] {symbol} - BB {len(bb_zones)}개 감지됨")
    #send_discord_debug(f"[BB] {symbol} - BB {len(bb_zones)}개 감지됨", "aggregated")
    return bb_zones