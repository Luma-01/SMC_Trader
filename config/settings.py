# config/settings.py

import requests

# 전략 전역 설정
RR = 2.0
SL_BUFFER = 0.005
CANDLE_LIMIT = 150
TIMEFRAMES = ['1m', '5m', '15m', '1h']
DEFAULT_LEVERAGE = 20

# 수동 레버리지 조정 (없으면 DEFAULT_LEVERAGE 적용됨)
CUSTOM_LEVERAGES = {
    "BTCUSDT": 10,
    "ETHUSDT": 15,
}

BINANCE_SPOT_URL = "https://api.binance.com"
BINANCE_FUTURES_URL = "https://fapi.binance.com"

def fetch_max_leverages():
    try:
        res = requests.get(f"{BINANCE_FUTURES_URL}/fapi/v1/leverageBracket")
        data = res.json()
        return {
            entry['symbol']: int(entry['brackets'][0]['initialLeverage'])
            for entry in data
        }
    except Exception as e:
        print(f"[ERROR] 최대 레버리지 조회 실패: {e}")
        return {}

def fetch_top_symbols(limit=10):
    info = requests.get(f"{BINANCE_SPOT_URL}/api/v3/exchangeInfo").json()
    tickers = requests.get(f"{BINANCE_SPOT_URL}/api/v3/ticker/24hr").json()

    usdt_symbols = [
        s for s in info['symbols']
        if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING' and s['isSpotTradingAllowed']
    ]

    volume_map = {t['symbol']: float(t['quoteVolume']) for t in tickers}
    sorted_symbols = sorted(usdt_symbols, key=lambda x: volume_map.get(x['symbol'], 0), reverse=True)[:limit]

    max_leverage_map = fetch_max_leverages()
    result = {}

    for s in sorted_symbols:
        symbol = s['symbol']
        lot_size = next(f for f in s['filters'] if f['filterType'] == 'LOT_SIZE')
        min_qty = float(lot_size['minQty'])

        custom_lev = CUSTOM_LEVERAGES.get(symbol, DEFAULT_LEVERAGE)
        max_lev = max_leverage_map.get(symbol, DEFAULT_LEVERAGE)
        applied_lev = min(custom_lev, max_lev)

        result[symbol] = {
            "base": s['baseAsset'],
            "minQty": min_qty,
            "leverage": applied_lev,
            "maxLeverage": max_lev
        }

    return result

# 실행 시 자동 불러오기
SYMBOLS = fetch_top_symbols()