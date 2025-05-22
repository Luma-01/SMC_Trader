# config/settings.py

import requests
from binance.client import Client
from dotenv import load_dotenv
import os
from notify.discord import send_discord_debug

load_dotenv()
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

client = Client(api_key, api_secret, tld='com')
client.API_URL = "https://fapi.binance.com/fapi"

RR = 2.0
SL_BUFFER = 0.005
CANDLE_LIMIT = 150
TIMEFRAMES = ['1m', '5m', '15m', '1h']
DEFAULT_LEVERAGE = 20

CUSTOM_LEVERAGES = {}

def fetch_max_leverages():
    try:
        data = client.futures_leverage_bracket()
        return {
            entry['symbol']: int(entry['brackets'][0]['initialLeverage'])
            for entry in data
        }
    except Exception as e:
        msg = f"❌ [BINANCE] 거래량 기준 심볼 조회 실패: {e}"
        print(msg)
        send_discord_debug(msg, "binance")
        return {}

def fetch_top_futures_symbols(limit=10):
    try:
        ticker = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/24hr").json()
        sorted_by_volume = sorted(ticker, key=lambda x: float(x['quoteVolume']), reverse=True)
        top_symbols = [s['symbol'] for s in sorted_by_volume if s['symbol'].endswith('USDT')][:limit]
        return top_symbols
    except Exception as e:
        msg = f"❌ [BINANCE] 거래량 기준 심볼 조회 실패: {e}"
        print(msg)
        send_discord_debug(msg, "binance")
        return []

def fetch_symbol_info(symbols):
    info = requests.get("https://api.binance.com/api/v3/exchangeInfo").json()
    all_symbols = {s['symbol']: s for s in info['symbols']}
    max_leverages = fetch_max_leverages()
    result = {}

    for symbol in symbols:
        if symbol not in all_symbols:
            msg = f"⚠️ [BINANCE] 심볼 누락: {symbol} - exchangeInfo 응답에 없음"
            print(msg)
            send_discord_debug(msg, "binance")
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
            "maxLeverage": max_lev,
            # 거래소 구분 후 타임프레임 설정
            "htf": "15m" if "_USDT" in symbol else "1h",
            "ltf": "1m" if "_USDT" in symbol else "5m"
        }

    return result

# 실행 시 자동 로딩
SYMBOLS = fetch_symbol_info(fetch_top_futures_symbols())

# ───────────────────────────── 추가 ─────────────────────────────
# 거래소별 심볼 테이블 분리
#  - Binance : BTCUSDT 형식 그대로 사용
#  - Gate.io : 주문 직전에만 BTC_USDT 로 변환하므로 여기선 그대로 둔다
SYMBOLS_BINANCE = SYMBOLS                       # dict 그대로 참조
SYMBOLS_GATE    = list(SYMBOLS.keys())          # ▶ 리스트면 set·len 등 사용 쉬움
# ───────────────────────────────────────────────────────────────