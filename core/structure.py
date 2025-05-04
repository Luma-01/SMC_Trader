# core/structure.py

import pandas as pd
from notify.discord import send_discord_debug

last_sent_structure: dict[str, str] = {}

def detect_structure(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.attrs.setdefault("symbol", "UNKNOWN")  # 없으면 기본값 설정
    df['prev_high'] = df['high'].shift(1)
    df['prev_low'] = df['low'].shift(1)
    df['structure'] = None

    symbol = df.attrs.get("symbol", "UNKNOWN")
    last_type = last_sent_structure.get(symbol)

    if len(df) < 3:
        print("[STRUCTURE] ❌ 캔들 수 부족 → 구조 분석 불가")
        send_discord_debug("[STRUCTURE] ❌ 캔들 수 부족 → 구조 분석 불가", "aggregated")
        df['structure'] = None
        return df
    
    i = len(df) - 1  # 마지막 캔들
    structure_type = None

    try:
        if (
            df['high'].iloc[i] > df['high'].iloc[i - 1]
            and df['low'].iloc[i] > df['low'].iloc[i - 1]
        ):
            df.at[df.index[i], 'structure'] = 'BOS_up'
            structure_type = 'BOS_up'
        elif (
            df['low'].iloc[i] < df['low'].iloc[i - 1]
            and df['high'].iloc[i] < df['high'].iloc[i - 1]
        ):
            df.at[df.index[i], 'structure'] = 'BOS_down'
            structure_type = 'BOS_down'
        elif (
            df['low'].iloc[i] > df['low'].iloc[i - 1]
            and df['high'].iloc[i - 2] > df['high'].iloc[i - 1]
        ):
            df.at[df.index[i], 'structure'] = 'CHoCH_up'
            structure_type = 'CHoCH_up'
        elif (
            df['high'].iloc[i] < df['high'].iloc[i - 1]
            and df['low'].iloc[i - 2] < df['low'].iloc[i - 1]
        ):
            df.at[df.index[i], 'structure'] = 'CHoCH_down'
            structure_type = 'CHoCH_down'
    except Exception as e:
        print(f"[STRUCTURE] 예외 발생: {e}")
        send_discord_debug(f"[STRUCTURE] 예외 발생: {e}", "aggregated")
        df['structure'] = None
        return df

    if structure_type:
        df.at[df.index[i], 'structure'] = structure_type
        if structure_type != last_type:
            print(f"[STRUCTURE] {symbol} → {structure_type} 발생 | 시각: {df['time'].iloc[i]}")
            send_discord_debug(f"[STRUCTURE] {symbol} → {structure_type} 발생 | 시각: {df['time'].iloc[i]}", "aggregated")
            last_sent_structure[symbol] = structure_type

    return df
