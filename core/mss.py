# core/mss.py

import pandas as pd
from typing import Optional, Dict
from core.structure import detect_structure  # 이미 만든 구조 분석기 사용
from notify.discord import send_discord_debug

def get_mss_and_protective_low(df: pd.DataFrame, direction: str) -> Optional[Dict]:
    """
    최근 MSS(BOS) 감지 후, MSS 직전의 스윙 로우(롱) 또는 스윙 하이(숏)를 보호선으로 반환

    direction: 'long' 또는 'short'
    """
    df_struct = detect_structure(df)
    df_struct = df_struct.dropna(subset=['structure'])

    if df_struct.empty:
        send_discord_debug(f"[MSS] ❌ 구조 데이터 없음 → MSS 판단 불가", "aggregated")
        return None

    # 가장 최근 BOS 방향 구조 탐색 (MSS)
    mss_idx = None
    for i in range(len(df_struct) - 1, 1, -1):
        row = df_struct.iloc[i]
        if direction == 'long' and row['structure'] == 'BOS_up':
            mss_idx = i
            break
        elif direction == 'short' and row['structure'] == 'BOS_down':
            mss_idx = i
            break

    if mss_idx is None:
        send_discord_debug(f"[MSS] ❌ {direction.upper()} MSS 미탐지 (BOS 구조 없음)", "aggregated")
        return None
    
    if mss_idx < 2:
        send_discord_debug(f"[MSS] ❌ MSS는 있으나 직전 스윙 기준부족 (mss_idx={mss_idx})", "aggregated")
        return None

    # MSS 발생 직전 캔들 범위
    df_before_mss = df_struct.iloc[:mss_idx]

    # 보호 저점/고점 결정
    if direction == 'long':
        protective_level = df_before_mss['low'].min()
    else:
        protective_level = df_before_mss['high'].max()
    send_discord_debug(
        f"[MSS] ✅ {direction.upper()} MSS 감지 | 보호선: {protective_level:.2f} @ {df_struct.iloc[mss_idx]['time']}",
        "aggregated"
    )
    
    return {
        "mss_time": df_struct.iloc[mss_idx]['time'],
        "protective_level": protective_level
    }
