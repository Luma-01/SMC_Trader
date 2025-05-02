# config/settings.py

import requests

RR = 2.0
SL_BUFFER = 0.005
CANDLE_LIMIT = 150
TIMEFRAMES = ['1m', '5m', '15m', '1h']
DEFAULT_LEVERAGE = 20

BINANCE_FUTURES_URL = "https://fapi.binance.com"

# 예: 'BTCUSDT': 10,  # 수동 설정 가능
CUSTOM_LEVERAGES = {
    "BTCUSDT": 30,
}

def fetch_max_leverages():
    try:
        res = requests.get(f"{BINANCE_FUTURES_URL}/fapi/v1/leverageBracket")
        data = res.json()
        if isinstance(data, dict) and "code" in data:
            raise Exception(f"API Error: {data}")
        return {
            entry['symbol']: int(entry['brackets'][0]['initialLeverage'])
            for entry in data
        }
    except Exception as e:
        print(f"[ERROR] 최대 레버리지 조회 실패: {e}")
        return {}

def fetch_top_futures_symbols(limit=10):
    try:
        ticker = requests.get(f"{BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr").json()
        sorted_by_volume = sorted(ticker, key=lambda x: float(x['quoteVolume']), reverse=True)
        top_symbols = [s['symbol'] for s in sorted_by_volume if s['symbol'].endswith('USDT')][:limit]
        return top_symbols
    except Exception as e:
        print(f"[ERROR] 거래량 기준 심볼 조회 실패: {e}")
        return []

def fetch_symbol_info(symbols):
    info = requests.get("https://api.binance.com/api/v3/exchangeInfo").json()
    all_symbols = {s['symbol']: s for s in info['symbols']}
    max_leverages = fetch_max_leverages()
    result = {}

    for symbol in symbols:
        if symbol not in all_symbols:
            continue
        s = all_symbols[symbol]
        lot_size = next(f for f in s['filters'] if f['filterType'] == 'LOT_SIZE')
        min_qty = float(lot_size['minQty'])

        custom_lev = CUSTOM_LEVERAGES.get(symbol, DEFAULT_LEVERAGE)
        max_lev = max_leverages.get(symbol, DEFAULT_LEVERAGE)
        applied_lev = min(custom_lev, max_lev)

        result[symbol] = {
            "base": s['baseAsset'],
            "minQty": min_qty,
            "leverage": applied_lev,
            "maxLeverage": max_lev
        }

    return result

# 최종 자동 로딩 심볼 목록
SYMBOLS = fetch_symbol_info(fetch_top_futures_symbols())