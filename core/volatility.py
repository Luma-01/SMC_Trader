# core/volatility.py
import pandas as pd
from typing import Optional
from config.settings import ATR_PERIOD  # 예: 14

def atr_pct(df: pd.DataFrame) -> Optional[float]:
    """
    HTF DataFrame → ATR(%) 를 계산해서 반환
    ------------------------------------------------
    * 기대 컬럼: high, low, close
    * 반환값  : (ATR / 최근 종가) × 100    ─ 소수점 %
               데이터가 부족하면 None
    """
    if df is None or len(df) < ATR_PERIOD + 2:
        return None

    # DataFrame 복사본 생성하여 SettingWithCopyWarning 방지
    df = df.copy()

    # ───────── True Range 계산 ─────────
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"]  - df["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder-style EMA(α = 1 / n)  → TA-Lib ATR 과 동일
    atr = tr.ewm(alpha=1 / ATR_PERIOD, min_periods=ATR_PERIOD).mean().iloc[-1]

    return float(atr / df["close"].iloc[-1] * 100)
