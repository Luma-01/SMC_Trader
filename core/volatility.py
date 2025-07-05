# core/volatility.py

import pandas as pd
from typing import Optional
from config.settings import ATR_PERIOD
try:
    import talib
    _ta = True
except ImportError:
    _ta = False

def atr_pct(df: pd.DataFrame) -> Optional[float]:
    """
    df :  columns = high, low, close   (HTF 1h DataFrame)
    return : ATR / close ×100  (%)
    """
    if df is None or len(df) < ATR_PERIOD + 2:
        return None

    if _ta:   # talib 사용 가능
        atr = talib.ATR(df['high'], df['low'], df['close'],
                        timeperiod=ATR_PERIOD).iloc[-1]
    else:     # 순수 pandas (EMA-type Wilder)
        tr = pd.concat([
            (df['high'] - df['low']),
            (df['high'] - df['close'].shift()).abs(),
            (df['low']  - df['close'].shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/ATR_PERIOD, min_periods=ATR_PERIOD).mean().iloc[-1]

    return float(atr / df['close'].iloc[-1] * 100)
