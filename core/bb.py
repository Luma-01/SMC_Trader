# core/bb.py
import pandas as pd
from typing import List, Dict
from notify.discord import send_discord_debug

def detect_bb(df: pd.DataFrame, ob_zones: List[Dict], max_rebound_candles: int = 3) -> List[Dict]:
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
        df_after = df[df['time'] > ob_time].reset_index(drop=True)

        invalidated = False
        invalid_index = None

        for i, row in df_after.iterrows():
            if ob_type == "bullish" and row['low'] < ob_low:
                invalidated = True
                invalid_index = i
                break
            elif ob_type == "bearish" and row['high'] > ob_high:
                invalidated = True
                invalid_index = i
                break

        if invalidated and invalid_index is not None:
            for j in range(invalid_index + 1, min(invalid_index + 1 + max_rebound_candles, len(df_after))):
                rebound = df_after.iloc[j]
                if ob_type == "bullish":
                    bb_zones.append({
                        "type": "bearish",
                        "high": rebound['high'],
                        "low": rebound['low'],
                        "time": rebound['time']
                    })
                    break
                elif ob_type == "bearish":
                    bb_zones.append({
                        "type": "bullish",
                        "high": rebound['high'],
                        "low": rebound['low'],
                        "time": rebound['time']
                    })
                    break

    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    print(f"[BB][{tf}] {symbol} - BB {len(bb_zones)}개 감지됨")
    return bb_zones