# core/ob.py
import pandas as pd
from notify.discord import send_discord_debug
from typing import List, Dict
from decimal import Decimal, ROUND_DOWN

def detect_ob(df: pd.DataFrame) -> List[Dict]:
    """
    정통 SMC 방식 Order Block 감지:
    - bullish: 하락 마감 음봉 뒤 상승 발생
    - bearish: 상승 마감 양봉 뒤 하락 발생
    """
    df = df.copy()
    ob_zones = []
    # displacement(변위) 캔들은 통상 1~3봉 안쪽을 봅니다
    MAX_DISPLACEMENT = 3

    # shadow(꼬리) 무시하고 body 영역만 zone 으로 저장
    def ob_body(candle):
        o, c = Decimal(str(candle["open"])), Decimal(str(candle["close"]))
        return (max(o, c), min(o, c))     # high, low (body extreme)

    for i in range(2, len(df) - MAX_DISPLACEMENT):
        c1 = df.iloc[i - 2]
        c2 = df.iloc[i - 1]
        high2, low2 = ob_body(c2)
        for j in range(1, MAX_DISPLACEMENT + 1):
            if i + j >= len(df):
                break
            c_next = df.iloc[i + j]

            # Bearish OB: 상승 후 하락 displacement
            if (
                c1["high"] < c2["high"]                      # 이전 봉 대비 고점 상승
                and c2["high"] > c_next["high"]             # 이후 봉 고점↓
                and c_next["close"] < c_next["open"]        # 하락 마감
            ):
                ob_zones.append({
                    "type": "bearish",
                    "high": float(high2),
                    "low": float(low2),
                    "time": c2['time']
                })
                break

            # Bullish OB: 하락 후 상승 displacement
            if (
                c1["low"] > c2["low"]
                and c2["low"] < c_next["low"]
                and c_next["close"] > c_next["open"]
            ):
                ob_zones.append({
                    "type": "bullish",
                    "high": float(high2),
                    "low": float(low2),
                    "time": c2['time']
                })
                break

    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    # 디버그 메시지는 가장 최근 1개만 출력 (딱 필요한 정보만)
    if ob_zones:
        last = ob_zones[-1]
        print(f"[OB][{tf}] {symbol} → {last['type'].upper()} {last['low']}~{last['high']} (총 {len(ob_zones)})")
    else:
        print(f"[OB][{tf}] {symbol} → 감지 없음")

    return ob_zones
