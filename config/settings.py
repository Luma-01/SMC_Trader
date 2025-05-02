# config/settings.py

RR = 2.0
SL_BUFFER = 0.005
TIMEFRAMES = ['1m', '5m', '15m', '1h']
CANDLE_LIMIT = 150

SYMBOLS = {
    'BTCUSDT': {
        'base': 'BTC',
        'minQty': 0.001,
        'leverage': 20,
        'maxLeverage': 20
    },
    'ETHUSDT': {
        'base': 'ETH',
        'minQty': 0.01,
        'leverage': 20,
        'maxLeverage': 20
    }
}
