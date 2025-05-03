# core/data_feed.py

import aiohttp
import asyncio
import time
import requests
from collections import defaultdict, deque
from datetime import datetime
from config.settings import SYMBOLS, TIMEFRAMES, CANDLE_LIMIT
from notify.discord import send_discord_debug

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
    failed = []
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                data = load_historical_candles(symbol, tf)
                candles[symbol][tf].extend(data)
            except Exception as e:
                failed.append(f"{symbol}-{tf}")
                send_discord_debug(f"[ERROR] 캔들 로딩 실패: {symbol}-{tf} → {e}", "binance")
    print(f"[HIST] 모든 심볼/타임프레임 캔들 로딩 완료. 실패: {failed if failed else '없음'}")
    send_discord_debug(f"[HIST] 모든 심볼/타임프레임 캔들 로딩 완료. 실패: {failed if failed else '없음'}", "binance")

# 2. 실시간 WebSocket 연결
async def stream_live_candles():
    stream_pairs = [
        f"{symbol.lower()}@kline_{tf}" for symbol in SYMBOLS for tf in TIMEFRAMES
    ]
    url = BINANCE_WS_URL + "/".join(stream_pairs)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(url) as ws:
                print("[WS] WebSocket connected.")
                send_discord_debug("[WS] WebSocket connected.", "binance")
                async for msg in ws:
                    data = msg.json()['data']
                    ...
        except Exception as e:
            send_discord_debug(f"[ERROR] WebSocket 연결 실패: {e}", "binance")

# 메인 실행 함수 (초기 로딩 + 실시간 연결)
async def start_data_feed():
    initialize_historical()
    await stream_live_candles()
