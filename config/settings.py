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

# ─── 상위 심볼 개수 & 오버슛 비율 상수 ─────────────────────────────
TOP_SYMBOL_LIMIT  = 10           # 최종적으로 사용할 상위 N개 심볼
OVERSHOOT_FACTOR = 2            # limit * OVERSHOOT_FACTOR 만큼 여유분 확보
# ──────────────────────────────────────────────────────────────────

def fetch_top_futures_symbols(
    limit: int    = TOP_SYMBOL_LIMIT,
    overshoot: int = TOP_SYMBOL_LIMIT * OVERSHOOT_FACTOR
):
    """
    ▸ 24h 거래량 상위 심볼을 (limit + overshoot) 만큼 가져온다.
      - exchangeInfo 에서 빠지는 심볼을 제외하고도 최종 10개를 확보하기 위함.
    """
    EXCLUDE_SYMBOLS = {"BTCUSDT"}  # ⛔ 제외할 심볼
    try:
        ticker = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr").json()
        sorted_by_volume = sorted(ticker, key=lambda x: float(x['quoteVolume']), reverse=True)
        top_symbols = []
        for s in sorted_by_volume:
            symbol = s['symbol']
            if symbol.endswith('USDT') and symbol not in EXCLUDE_SYMBOLS:
                top_symbols.append(symbol)
            if len(top_symbols) >= limit + overshoot:
                break
        return top_symbols
    
    except Exception as e:
        msg = f"❌ [BINANCE] 거래량 기준 심볼 조회 실패: {e}"
        print(msg)
        send_discord_debug(msg, "binance")
        return []

def fetch_symbol_info(
    symbols,
    required: int = TOP_SYMBOL_LIMIT
):
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

    # ▸ 부족하면 그대로, 넘치면 앞에서 required 개만 잘라서 반환
    return dict(list(result.items())[:required])

# ─── 상위 심볼 한 번에 뽑아주는 래퍼 ───────────────────────────────
def fetch_top_symbols(limit: int = TOP_SYMBOL_LIMIT,
                      overshoot_factor: int = OVERSHOOT_FACTOR):
    raw = fetch_top_futures_symbols(
        limit=limit,
        overshoot=limit * overshoot_factor
    )
    return fetch_symbol_info(raw, required=limit)
# ──────────────────────────────────────────────────────────────────

# 실행 시 자동 로딩
SYMBOLS = fetch_top_symbols()

# ───────────────────────────── 추가 ─────────────────────────────
# 거래소별 심볼 테이블 분리
#  - Binance : BTCUSDT 형식 그대로 사용
#  - Gate.io : 주문 직전에만 BTC_USDT 로 변환하므로 여기선 그대로 둔다
SYMBOLS_BINANCE = SYMBOLS                       # dict 그대로 참조
SYMBOLS_GATE    = list(SYMBOLS.keys())          # ▶ 리스트면 set·len 등 사용 쉬움
# ───────────────────────────────────────────────────────────────