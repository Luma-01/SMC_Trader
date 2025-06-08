# core/ob.py
import pandas as pd
from notify.discord import send_discord_debug
# ─────────────────────────────────────────────────────────
#  OB 리스트 후처리 : 겹치는 영역만 추출
#  - N 개의 OB 가 서로 겹치면, 교집합(high=min(high), low=max(low)) 만 남김
#  - 겹치지 않는 OB 는 그대로 유지
# ─────────────────────────────────────────────────────────
#  ⓘ 패치 포인트 : detect_ob() → 마지막에 refine_overlaps() 호출
# ─────────────────────────────────────────────────────────
from typing import List, Dict, Tuple
from decimal import Decimal

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
    # ───────────────────────────────────────────
    # ① 겹치는 OB 교집합으로 축소
    # ───────────────────────────────────────────
    ob_zones = refine_overlaps(ob_zones)

    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    # 디버그 메시지는 가장 최근 1개만 출력 (딱 필요한 정보만)
    if ob_zones:
        last = ob_zones[-1]
        print(f"[OB][{tf}] {symbol} → {last['type'].upper()} {last['low']}~{last['high']} (총 {len(ob_zones)})")
    else:
        print(f"[OB][{tf}] {symbol} → 감지 없음")

    # ───────── 중복-알림 차단 ──────────
    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf     = df.attrs.get("tf", "?")
    key    = (symbol, tf)

    _seen = _OB_CACHE.setdefault(key, set())   # 전역 dict  { (sym,tf): set() }
    fresh = []
    for z in ob_zones:
        sig = (round(z["low"], 8), round(z["high"], 8), z["type"])
        if sig in _seen:
            continue                # 이미 알림 → 건너뜀
        _seen.add(sig)
        fresh.append(z)

    # ① fresh 로 잡힌 OB 만 알림
    for z in fresh[-5:]:
        msg = (
            f"[OB] {symbol} ({tf}) NEW {z['type'].upper()}  "
            f"{z['low']} ~ {z['high']}  |  {z['time']:%Y-%m-%d %H:%M}"
        )
        print(msg)
        #send_discord_debug(msg, "aggregated")          # 두 번째 인자는 원하는 태그

    # ② 전략단에는 교집합 처리된 OB 리스트를 넘긴다
    return ob_zones


# ─────────────────────────────────────────────────────────
#  NEW : overlap refiner
# ─────────────────────────────────────────────────────────
def _intersects(a: Tuple[float, float], b: Tuple[float, float]) -> bool:
    """두 구간이 겹치는지 여부만 판정"""
    return not (a[1] < b[0] or b[1] < a[0])

def refine_overlaps(obs: List[Dict]) -> List[Dict]:
    """
    ▸ 겹치는 OB 들만 모아 **교집합**(가장 좁은 범위) 으로 치환  
    ▸ 타입(bullish/bearish) 이 다른 경우는 별개로 취급  
    """
    refined: List[Dict] = []
    used = [False] * len(obs)

    for i, ob in enumerate(obs):
        if used[i]:
            continue

        # 현재 OB 와 겹치는 동일-방향 OB 모으기
        overlaps = [ob]
        for j in range(i + 1, len(obs)):
            if used[j]:
                continue
            other = obs[j]
            if ob["type"] == other["type"] and _intersects((ob["low"], ob["high"]),
                                                           (other["low"], other["high"])):
                overlaps.append(other)
                used[j] = True

        # 1 개뿐이면 그대로, 2 개 이상이면 교집합으로 축소
        if len(overlaps) == 1:
            refined.append(ob)
        else:
            low  = max(o["low"]  for o in overlaps)
            high = min(o["high"] for o in overlaps)
            if low < high:                       # 유효 교집합
                base = dict(ob)                  # 아무 OB 하나 복사
                base.update({
                    "low":  low,
                    "high": high,
                    "kind": "ob_overlap"         # 표식 → 전략단에서 구분 가능
                })
                refined.append(base)

    # 폭( high-low ) 기준으로 작은 것부터 정렬 (선택)
    refined.sort(key=lambda x: x["high"] - x["low"])
    return refined

# ───────── 모듈 전역 캐시  ─────────
_OB_CACHE: dict[tuple[str, str], set[tuple]] = {}