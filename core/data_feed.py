# core/data_feed.py

import aiohttp
import asyncio
import time
import requests
from collections import defaultdict, deque
from datetime import datetime
from config.settings import SYMBOLS, TIMEFRAMES, CANDLE_LIMIT

BINANCE_REST_URL = "https://api.binance.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream?streams="

# 캔들 저장소: {symbol: {timeframe: deque}}
candles = defaultdict(lambda: defaultdict(lambda: deque(maxlen=CANDLE_LIMIT)))

# 1. 과거 캔들 로딩 (REST)
def load_historical_candles(symbol: str, interval: str, limit: int = CANDLE_LIMIT):
    url = f"{BINANCE_REST_URL}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    response = requests.get(url, params=params)
    data = response.json()
    return [
        {
            "time": datetime.fromtimestamp(d[0] / 1000),
            "open": float(d[1]),
            "high": float(d[2]),
            "low": float(d[3]),
            "close": float(d[4]),
            "volume": float(d[5])
        } for d in data
    ]

def initialize_historical():
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                data = load_historical_candles(symbol, tf)
                candles[symbol][tf].extend(data)
                print(f"[HIST] {symbol} {tf} loaded ({len(data)} candles)")
            except Exception as e:
                print(f"[ERROR] loading {symbol}-{tf}: {e}")

# 2. 실시간 WebSocket 연결
async def stream_live_candles():
    stream_pairs = [
        f"{symbol.lower()}@kline_{tf}" for symbol in SYMBOLS for tf in TIMEFRAMES
    ]
    url = BINANCE_WS_URL + "/".join(stream_pairs)

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as ws:
            print("[WS] WebSocket connected.")
            async for msg in ws:
                data = msg.json()['data']
                symbol = data['s']
                tf = data['k']['i']
                candle = {
                    "time": datetime.fromtimestamp(data['k']['t'] / 1000),
                    "open": float(data['k']['o']),
                    "high": float(data['k']['h']),
                    "low": float(data['k']['l']),
                    "close": float(data['k']['c']),
                    "volume": float(data['k']['v'])
                }
                candles[symbol][tf].append(candle)

# 메인 실행 함수 (초기 로딩 + 실시간 연결)
async def start_data_feed():
    initialize_historical()
    await stream_live_candles()
