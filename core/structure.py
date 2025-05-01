import pandas as pd

def detect_structure(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['prev_high'] = df['high'].shift(1)
    df['prev_low'] = df['low'].shift(1)
    df['structure'] = None

    for i in range(2, len(df)):
        if (
            df['high'].iloc[i] > df['high'].iloc[i - 1]
            and df['low'].iloc[i] > df['low'].iloc[i - 1]
        ):
            df.at[df.index[i], 'structure'] = 'BOS_up'

        elif (
            df['low'].iloc[i] < df['low'].iloc[i - 1]
            and df['high'].iloc[i] < df['high'].iloc[i - 1]
        ):
            df.at[df.index[i], 'structure'] = 'BOS_down'

        elif (
            df['low'].iloc[i] > df['low'].iloc[i - 1]
            and df['high'].iloc[i - 2] > df['high'].iloc[i - 1]
        ):
            df.at[df.index[i], 'structure'] = 'CHoCH_up'

        elif (
            df['high'].iloc[i] < df['high'].iloc[i - 1]
            and df['low'].iloc[i - 2] < df['low'].iloc[i - 1]
        ):
            df.at[df.index[i], 'structure'] = 'CHoCH_down'

    return df[['time', 'open', 'high', 'low', 'close', 'structure']]
