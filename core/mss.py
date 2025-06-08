# core/mss.py

import pandas as pd
from typing import Optional, Dict
from core.structure import detect_structure  # 이미 만든 구조 분석기 사용
from notify.discord import send_discord_debug

def get_mss_and_protective_low(df: pd.DataFrame, direction: str) -> Optional[Dict]:
    """
    최근 MSS(BOS) 감지 후 MSS 직전 스윙로우/스윙하이를 보호선으로 돌려줌
    direction: 'long' or 'short'
    """
    df_struct = detect_structure(df).dropna(subset=['structure'])

    if df_struct.empty:
        print("[MSS] 구조 데이터 없음 → MSS 판단 불가")
        return None

    # ───── 최근 BOS(= MSS) 찾기 ───────────────────
    df_struct = df_struct.reset_index(drop=True)   # iloc 인덱스 안전
    bos_tag   = 'BOS_up'  if direction == 'long'  else 'BOS_down'

    mss_idx = df_struct[df_struct['structure'] == bos_tag].last_valid_index()
    if mss_idx is None or mss_idx < 2:            # 직전 스윙 포인트 부족
        print(f"[MSS] {direction.upper()} MSS 미탐지/기준부족")
        return None

    # ───── 보호선 계산 ────────────────────────────
    pre_mss = df_struct.loc[:mss_idx-1]
    protective = (
        pre_mss['low'].min()   if direction == 'long'
        else pre_mss['high'].max()
    )

    print(f"[MSS] {direction.upper()} MSS 발견 | 보호선={protective:.4f} @ {df_struct.loc[mss_idx,'time']}")
    send_discord_debug(
        f"[MSS] {direction.upper()} MSS | 보호선 {protective:.4f}", "aggregated"
    )

    return {"mss_time": df_struct.loc[mss_idx, 'time'],
            "protective_level": protective}
