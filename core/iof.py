# core/iof.py

import pandas as pd
from datetime import datetime, timezone
from config.settings import ENTRY_METHOD, LTF_TF   # LTF_TF 추가 가져오기
from core.structure import detect_structure
from core.ob import detect_ob
from core.bb import detect_bb
from core.mss import get_mss_and_protective_low
from notify.discord import send_discord_debug
from typing import Tuple, Optional, Dict
from decimal import Decimal
from collections import defaultdict

# ─────────────────────────────────────────────────────────────
#  ✅  무효(소멸)-블록 캐시
#      INVALIDATED_BLOCKS[symbol] = { (kind, tf, high, low), … }
# ─────────────────────────────────────────────────────────────

INVALIDATED_BLOCKS: defaultdict[str, set[tuple]] = defaultdict(set)

# ───── 헬퍼: 진행-중 캔들 제거 ─────────────────────────
def _drop_unclosed(df: pd.DataFrame, tf_minutes: int) -> pd.DataFrame:
    if df.empty:
        return df
    last = df["time"].iloc[-1].to_pydatetime().replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - last).total_seconds() < tf_minutes * 60:
        return df.iloc[:-1]
    return df

def mark_invalidated(symbol: str,
                     kind: str, tf: str,
                     high: float, low: float) -> None:
    """
    가격이 블록(OB·BB)을 ‘완전히’ 돌파해 무효화됐을 때 호출.
    이후 엔트리 스캔 단계에서 해당 블록이 자동으로 제외된다.
    """
    INVALIDATED_BLOCKS[symbol].add((kind, tf, high, low))


def is_invalidated(symbol: str,
                   kind: str, tf: str,
                   high: float, low: float) -> bool:
    """지정 블록이 이미 무효화됐는지 여부"""
    return (kind, tf, high, low) in INVALIDATED_BLOCKS[symbol]

_LAST_OB_TIME: dict[tuple[str, str], datetime]          = {}
_OB_CACHE_HTF: dict[tuple[str, str], tuple]            = {}

#   True/False , 'long'|'short'|None ,  존 dict 또는 None
def is_iof_entry(
        htf_df: pd.DataFrame,
        ltf_df: pd.DataFrame,
        tick_size: Decimal
) -> Tuple[bool, Optional[str], Optional[Dict]]:
    trigger_zone = None        # ← 돌려줄 존 정보
    symbol = htf_df.attrs.get("symbol", "UNKNOWN")
    tf = htf_df.attrs.get("tf", "?")
    
    # 1. HTF 구조 판단
    htf_struct = detect_structure(htf_df)
    if htf_struct is None or not isinstance(htf_struct, pd.DataFrame) or 'structure' not in htf_struct.columns:
        print(f"[IOF] [{symbol}-{tf}] ❌ detect_structure() 반환 오류 → 진입 판단 불가")
        return False, None, None
    structure_series = htf_struct['structure'].dropna()
    if structure_series.empty:
        print(f"[IOF] [{symbol}-{tf}] ❌ 구조 데이터 없음 → 진입 판단 불가")
        return False, None, None
    recent = structure_series.iloc[-1]

    # Bias 판단 (기준: 구조의 마지막 값)
    bias = None
    if recent == 'BOS_up':
        bias = 'LONG'
    elif recent == 'BOS_down':
        bias = 'SHORT'
    elif recent.startswith('CHoCH'):
        bias = 'NONE'
    print(f"[BIAS] [{symbol}-{tf}] HTF 구조 기준 Bias = {bias} (최근 구조: {recent})")
    #send_discord_debug(f"[BIAS] HTF 구조 기준 Bias = {bias} (최근 구조: {recent})", "aggregated")

    if recent in ['BOS_up', 'CHoCH_up', 'OB_Break_up']:
        direction = 'long'
    elif recent in ['BOS_down', 'CHoCH_down', 'OB_Break_down']:
        direction = 'short'
    else:
        print(f"[IOF] [{symbol}-{tf}] ❌ 최근 구조 신호 미충족 → 최근 구조: {recent}")
        return False, None, None

    if bias in ['LONG', 'SHORT']:
        if bias.lower() == direction:
            print(f"[IOF] [{symbol}-{tf}] ✅ Bias와 진입 방향 일치 → Bias={bias}, Direction={direction}")
            #send_discord_debug(f"[IOF] ✅ Bias와 진입 방향 일치 → Bias={bias}, Direction={direction}", "aggregated")
        else:
            print(f"[IOF] [{symbol}-{tf}] ⚠️ Bias와 진입 방향 불일치 → Bias={bias}, Direction={direction}")
            #send_discord_debug(f"[IOF] ⚠️ Bias와 진입 방향 불일치 → Bias={bias}, Direction={direction}", "aggregated")

    # current_price 직접 정의 (PD ZONE 비활 임시 테스트용)
    if ltf_df.empty or 'close' not in ltf_df.columns or ltf_df['close'].dropna().empty:
        print("[IOF] ❌ LTF 종가 없음")
        return False, direction, None

    current_price = Decimal(str((ltf_df['high'].iloc[-1] + ltf_df['low'].iloc[-1]) / 2))
    current_price = Decimal(str(current_price)).quantize(tick_size)

    buffer = tick_size * 10  # ✅ 진입 완화용 버퍼 설정
    near_buffer = tick_size * 10  # ✅ 근접 로그용 완화 조건

    # ---------------------------------------------------------------------
    # 3-A)  ❖  HTF OB/BB 존 안에 있는지 먼저 확인
    # ---------------------------------------------------------------------
    IN_HTF_ZONE = False
    last_htf_time = htf_df["time"].iloc[-1]      # 마지막 완결 15m 캔들 시각

    cache_key = (symbol, tf)
    if _LAST_OB_TIME.get(cache_key) != last_htf_time:
        # ① 15 m 캔들이 새로 닫혔을 때만 HTF OB/BB 재계산
        htf_ob = detect_ob(htf_df)
        htf_bb = detect_bb(htf_df, htf_ob)
        _OB_CACHE_HTF[cache_key] = (last_htf_time, htf_ob, htf_bb)
        _LAST_OB_TIME[cache_key] = last_htf_time
    else:
        # ② 직전 계산값 재사용
        _, htf_ob, htf_bb = _OB_CACHE_HTF.get(cache_key, (None, [], []))

    # ── 모든 경우에 대해 None 방지 & 디버그 출력 ─────────────────────────────
    htf_ob = htf_ob or []
    htf_bb = htf_bb or []
    print(f"[DEBUG] {symbol}-{tf}  HTF_OB={len(htf_ob)}  HTF_BB={len(htf_bb)}")

    LOOKBACK_HTF = 20          # 최근 HTF 존 n개만 검사

    def _in_zone(z):
        low  = Decimal(str(z['low'])).quantize(tick_size)
        high = Decimal(str(z['high'])).quantize(tick_size)
        return (low - buffer) <= current_price <= (high + buffer)

    def zone_dir(z):                     # 존 타입 → 매매방향
        return 'long' if z['type'] == 'bullish' else 'short'

    # OB
    for z in reversed(htf_ob[-LOOKBACK_HTF:]):
        if _in_zone(z):
            IN_HTF_ZONE = True
            trigger_zone = {"kind": "ob_htf", **z}
            direction = zone_dir(z)
            print(f"[DEBUG] Hit HTF-OB  → direction set to {direction}")
            if ENTRY_METHOD == "zone_or_mss":
                return True, direction, trigger_zone   # ◆ MSS 컨펌 생략 모드
            break                                      # → and_mss 모드면 계속

    # BB (OB에서 못 찾았을 때만)
    if (not IN_HTF_ZONE):
        for z in reversed(htf_bb[-LOOKBACK_HTF:]):
            if _in_zone(z):
                IN_HTF_ZONE = True
                trigger_zone = {"kind": "bb_htf", **z}
                direction = zone_dir(z)
                print(f"[DEBUG] Hit HTF-BB  → direction set to {direction}")
                if ENTRY_METHOD == "zone_or_mss":
                    return True, direction, trigger_zone
                break

    # ──────────────────────────────────────────────────────────
    #  HTF 존 OUT 이면서 zone_or_mss 모드?  →  LTF MSS 단독 체크
    # ──────────────────────────────────────────────────────────
    # ① LTF_TF 가 '5m', '15m', '1h' 등 어떤 단위이든 분으로 환산
    unit = LTF_TF[-1].lower()      # 'm' or 'h'
    val  = int(LTF_TF[:-1])
    tf_minutes = val * (60 if unit == "h" else 1)

    if (not IN_HTF_ZONE) and ENTRY_METHOD == "zone_or_mss":
        ltf_df = _drop_unclosed(ltf_df, tf_minutes)

        ltf_struct_df = detect_structure(ltf_df, use_wick=False)
        last_structs  = ltf_struct_df['structure'].dropna()
        if last_structs.empty:
            return False, direction, None
        last_struct = last_structs.iloc[-1]

        need_long  = last_struct in ('BOS_up', 'CHoCH_up')
        need_short = last_struct in ('BOS_down', 'CHoCH_down')

        if (direction == 'long' and need_long) or (direction == 'short' and need_short):
            print(f"[ENTRY] MSS-only trigger ({last_struct}) → zone_or_mss")
            send_discord_debug(f"[ENTRY] MSS-only trigger → {last_struct}", "aggregated")
            # ── MSS 보호선 계산 (몸통 기준, 재진입 카운터 영향 X)
            mss = get_mss_and_protective_low(ltf_df, direction, use_wick=False, reentry_limit=999)
            prot = mss["protective_level"] if mss else None
            return True, direction, {"kind": "mss_only", "protective": prot}
        # MSS도 불일치면 진입 안 함
        return False, direction, None

    # ──────────────────────────────────────────────────────────
    # 여기서부터는 HTF 존은 이미 통과 → LTF MSS 컨펌 필요
    # (ENTRY_METHOD == 'zone_and_mss' 인 경우)
    # ──────────────────────────────────────────────────────────

    # ---------------------------------------------------------------------
    # 3-B)  ❖  LTF 구조 컨펌 (BOS / CHoCH 방향 일치)
    # ---------------------------------------------------------------------
    ltf_df = _drop_unclosed(ltf_df, tf_minutes)
    ltf_struct_df = detect_structure(ltf_df, use_wick=False)
    recent_structs = ltf_struct_df['structure'].dropna()
    if recent_structs.empty:
        return False, direction, None
    last_struct = recent_structs.iloc[-1]

    need_long  = last_struct in ('BOS_up', 'CHoCH_up')
    need_short = last_struct in ('BOS_down', 'CHoCH_down')

    if (direction == 'long'  and not need_long) or \
       (direction == 'short' and not need_short):
        # 컨펌 미달 → 아직 진입하지 않음
        return False, direction, None

    print(f"[CONFIRM] LTF 구조 컨펌 완료 → {last_struct}")
    #send_discord_debug(f"[CONFIRM] LTF 구조 컨펌 완료 → {last_struct}", "aggregated")

    # 여기까지 왔으면 HTF 존 + LTF BOS/CHoCH 모두 OK → 진입
    return True, direction, trigger_zone
