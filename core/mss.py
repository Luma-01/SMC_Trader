# core/mss.py

import pandas as pd
import numpy as np
from typing import Optional, Dict
from core.structure import detect_structure
from notify.discord import send_discord_debug

# 보호선별 재진입 카운터
REENTRY_COUNT: dict[tuple[str, float], int] = {}

def get_mss_and_protective_low(
    df: pd.DataFrame,
    direction: str,
    *,
    atr_window: int = 14,
    use_wick: bool = False,
    reentry_limit: int = 2,
) -> Optional[Dict]:
    """
    최근 MSS(BOS) 감지 후 MSS 직전 스윙로우/스윙하이를 보호선으로 돌려줌
    direction: 'long' or 'short'
    """
    # ── 구조 리스트 (NaN 제외) ─────────────────────
    df_struct = detect_structure(df, use_wick=use_wick).dropna(subset=['structure'])

    # 몸통 기준일 때 원본 df에도 body_high/low 컬럼이 없으면 생성
    if not use_wick and 'body_high' not in df.columns:
        df['body_high'] = df[['open', 'close']].max(axis=1)
        df['body_low']  = df[['open', 'close']].min(axis=1)

    hi = 'high' if use_wick else 'body_high'
    lo = 'low'  if use_wick else 'body_low'

    if df_struct.empty:
        print("[MSS] 구조 데이터 없음 → MSS 판단 불가")
        return None

    # ───── 최근 BOS(= MSS) 찾기 ───────────────────
    bos_tag = 'BOS_up' if direction == 'long' else 'BOS_down'
    mss_idx = df_struct[df_struct['structure'] == bos_tag].last_valid_index()  # <― 원본 index(label)

    if mss_idx is None:
        print(f"[MSS] {direction.upper()} MSS 미탐지/기준부족")
        return None

    # df_struct 내 위치(숫자 idx) → 보호선 계산용
    mss_pos = df_struct.index.get_loc(mss_idx)
    
    # ───── BOS 폭 & ATR 필터 ──────────────────────
    #   • BOS 폭이 0.8 × ATR14 이상일 때만 MSS 인정
    # ------------------------------------------------
    # ATR 계산(간단 True Range)
    df['prev_close'] = df['close'].shift(1)
    tr = pd.concat(
        [
            df[hi] - df[lo],
            (df[hi] - df['prev_close']).abs(),
            (df[lo] - df['prev_close']).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=atr_window).mean()

    bos_range = df.loc[mss_idx, hi] - df.loc[mss_idx, lo]
    atr_val   = atr.loc[mss_idx]
    if np.isnan(atr_val) or bos_range < 0.8 * atr_val:
        print(f"[MSS] {direction.upper()} MSS BOS폭 {bos_range:.2f} < 0.8×ATR({atr_val:.2f}) → 패스")
        return None

    # ───── 보호선 계산 ────────────────────────────
    # MSS 직전 최근 3~5개 스윙 포인트만 체크하도록 컷오프
    window = 5
    pre_mss = df_struct.iloc[max(0, mss_pos - window): mss_pos]
    protective = pre_mss[lo].min() if direction == 'long' else pre_mss[hi].max()

    # ───── 재진입 제한 ────────────────────────────
    symbol = df.attrs.get("symbol", "UNKNOWN")
    key    = (symbol, round(protective, 8))  # float 키 정규화
    if REENTRY_COUNT.get(key, 0) >= reentry_limit:
        print(f"[MSS] {symbol} 보호선 {protective:.4f} → 재진입 한도({reentry_limit}) 초과")
        return None
    REENTRY_COUNT[key] = REENTRY_COUNT.get(key, 0) + 1
    print(
        f"[MSS] {direction.upper()} MSS PASS | BOS폭 {bos_range:.2f} (ATR {atr_val:.2f}) "
        f"→ 보호선 {protective:.4f} @ {df_struct.loc[mss_idx,'time']}"
    )
    send_discord_debug(
        f"[MSS] {direction.upper()} MSS | 보호선 {protective:.4f}", "aggregated"
    )

    return {"mss_time": df_struct.loc[mss_idx, 'time'],
            "protective_level": protective}
