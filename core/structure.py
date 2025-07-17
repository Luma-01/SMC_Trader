# core/structure.py

import pandas as pd
from core.ob import detect_ob
from notify.discord import send_discord_debug

last_sent_structure: dict[tuple[str, str], tuple[str, pd.Timestamp]] = {}

def detect_structure(df: pd.DataFrame, *, use_wick: bool = True) -> pd.DataFrame:
    df = df.copy()
    df.attrs.setdefault("symbol", "UNKNOWN")  # 없으면 기본값 설정
    df.attrs.setdefault("tf", "?")  # 타임프레임 기본값 설정
    # ── 선택된 기준(몸통 vs 꼬리)에 따라 고·저 컬럼 매핑
    if not use_wick:
        df.loc[:, 'body_high'] = df[['open', 'close']].max(axis=1)
        df.loc[:, 'body_low'] = df[['open', 'close']].min(axis=1)
        hi, lo = 'body_high', 'body_low'
    else:
        hi, lo = 'high', 'low'

    df.loc[:, 'prev_high'] = df[hi].shift(1)
    df.loc[:, 'prev_low'] = df[lo].shift(1)
    df.loc[:, 'structure'] = None

    symbol = df.attrs.get("symbol", "UNKNOWN")
    tf = df.attrs.get("tf", "?")
    last_type, last_time = last_sent_structure.get((symbol, tf), (None, None))

    if len(df) < 3:
        print("[STRUCTURE] ❌ 캔들 수 부족 → 구조 분석 불가")
        send_discord_debug("[STRUCTURE] ❌ 캔들 수 부족 → 구조 분석 불가", "aggregated")
        df.loc[:, 'structure'] = None
        return df
    
    #print(f"[STRUCTURE DEBUG] {symbol} ({tf}) 구조 분석 시작 → 캔들 수: {len(df)}")
    structure_window_start = len(df) - 30

    structure_type = None
    structure_time = None
    for i in range(structure_window_start, len(df)):
        try:
            stype = None
            if df[hi].iloc[i] > df[hi].iloc[i - 1] and df[lo].iloc[i] > df[lo].iloc[i - 1]:
                stype = 'BOS_up'
            elif df[lo].iloc[i] < df[lo].iloc[i - 1] and df[hi].iloc[i] < df[hi].iloc[i - 1]:
                stype = 'BOS_down'
            elif df[lo].iloc[i] > df[lo].iloc[i - 1] and df[hi].iloc[i - 2] > df[hi].iloc[i - 1]:
                stype = 'CHoCH_up'
            elif df[hi].iloc[i] < df[hi].iloc[i - 1] and df[lo].iloc[i - 2] < df[lo].iloc[i - 1]:
                stype = 'CHoCH_down'

            if stype:
                df.loc[df.index[i], 'structure'] = stype
                structure_type = stype
                structure_time = df['time'].iloc[i]
        except Exception as e:
            print(f"[STRUCTURE] 예외 발생 (index={i}): {e}")
            continue

    # 마지막 구조만 알림
    # ────────────────────────────── ★ OB Break 탐지 ──────────────────────────────
    try:
        ob_list = detect_ob(df)
        if ob_list:
            last_ob   = ob_list[-1]
            last_px   = df["close"].iloc[-1]

            if last_ob["type"] == "bullish" and last_px < last_ob["low"]:
                structure_type = "OB_Break_down"
                structure_time = df["time"].iloc[-1]
                df.loc[df.index[-1], "structure"] = structure_type

            elif last_ob["type"] == "bearish" and last_px > last_ob["high"]:
                structure_type = "OB_Break_up"
                structure_time = df["time"].iloc[-1]
                df.loc[df.index[-1], "structure"] = structure_type
    except Exception:
        pass

    # ────────────────────────────────────────────────────────────────────────────
    if structure_type and ((structure_type, structure_time) != last_sent_structure.get((symbol, tf))):
        log_msg = f"[STRUCTURE] {symbol} ({tf}) → {structure_type} 발생 | 시각: {structure_time}"
        print(log_msg)
        #send_discord_debug(log_msg, "aggregated")
        last_sent_structure[(symbol, tf)] = (structure_type, structure_time)


    #recent_structs = df['structure'].dropna().tail(3).tolist()
    #print(f"[STRUCTURE DEBUG] {symbol} ({tf}) → 최근 구조 3개: {recent_structs}")

    return df
