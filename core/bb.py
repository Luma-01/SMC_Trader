# core/bb.py
import pandas as pd
from typing import List, Dict
from notify.discord import send_discord_debug
from decimal import Decimal, ROUND_DOWN

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
        ob_high = Decimal(str(ob['high']))
        ob_low = Decimal(str(ob['low']))
        ob_time = ob['time']
        df_after = df[df['time'] > ob_time].reset_index(drop=True)
        invalid_index = None      # OB 무효화된 봉 인덱스

        for i, row in df_after.iterrows():
            if ob_type == "bullish" and row['low'] < ob_low:
                invalidated = True
                invalid_index = i
                break
            elif ob_type == "bearish" and row['high'] > ob_high:
                invalidated = True
                invalid_index = i
                break

        if invalid_index is not None:
            # 무효화 직후 max_rebound_candles 이내에 반전 확인
            for j in range(
                invalid_index + 1,
                min(invalid_index + 1 + max_rebound_candles, len(df_after))
            ):
                rebound = df_after.iloc[j]
                high = Decimal(str(rebound['high']))
                low = Decimal(str(rebound['low']))
                if ob_type == "bullish":
                    bb_zones.append({
                        "type": "bearish",
                        "high": float(high),
                        "low": float(low),
                        "time": rebound['time']
                    })
                    break
                elif ob_type == "bearish":
                    bb_zones.append({
                        "type": "bullish",
                        "high": float(high),
                        "low": float(low),
                        "time": rebound['time']
                    })
                    break

    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    if bb_zones:
        last = bb_zones[-1]
        print(f"[BB][{tf}] {symbol} → {last['type'].upper()} {last['low']}~{last['high']} (총 {len(bb_zones)})")
    else:
        print(f"[BB][{tf}] {symbol} → 감지 없음")

    # ───────── 중복-알림 차단 ──────────
    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf     = df.attrs.get("tf", "?")
    key    = (symbol, tf)

    _seen = _BB_CACHE.setdefault(key, set())   # 전역 dict  { (sym,tf): set() }
    fresh = []
    for z in bb_zones:
        sig = (round(z["low"], 8), round(z["high"], 8), z["type"])
        if sig in _seen:
            continue                # 이미 알림 → 건너뜀
        _seen.add(sig)
        fresh.append(z)

    # ① fresh 로 잡힌 BB 만 알림
    for z in fresh[-5:]:
        msg = (
            f"[BB] {symbol} ({tf}) NEW {z['type'].upper()}  "
            f"{z['low']} ~ {z['high']}  |  {z['time']:%Y-%m-%d %H:%M}"
        )
        print(msg)
        #send_discord_debug(msg, "aggregated")          # 두 번째 인자는 원하는 태그

    # ② 전략에는 전체 OB 리스트를 넘긴다
    return bb_zones

# ───────── 모듈 전역 캐시  ─────────
_BB_CACHE: dict[tuple[str, str], set[tuple]] = {}