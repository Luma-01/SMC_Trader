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

    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(f"{symbol}-{interval} 캔들 로딩 실패 또는 빈 응답")

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
    total = 0
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                data = load_historical_candles(symbol, tf)
                candles[symbol][tf].extend(data)
                total += 1
            except Exception as e:
                failed.append(f"{symbol}-{tf}")
                send_discord_debug(f"❌ [BINANCE] 캔들 로딩 실패: {symbol}-{tf} → {e}", "binance")
    msg = (
        f"📊 [BINANCE] 캔들 로딩 완료\n"
        f" - 총 요청: {total}\n"
        f" - 실패: {len(failed)}\n"
        f" - 실패 목록: {', '.join(failed) if failed else '없음'}"
    )
    print(msg)
    send_discord_debug(msg, "binance")

# 2. 실시간 WebSocket 연결
async def stream_live_candles():
    stream_pairs = [
        f"{symbol.lower()}@kline_{tf}" for symbol in SYMBOLS for tf in TIMEFRAMES
    ]
    url = BINANCE_WS_URL + "/".join(stream_pairs)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(url) as ws:
                print("✅ [WS] Binance WebSocket 연결 성공!")
                send_discord_debug("✅ [BINANCE] WebSocket 연결 성공!", "binance")
                async for msg in ws:
                    raw = msg.json()
                    data = raw['data']
                    stream = raw['stream']  # e.g., btcusdt@kline_1m
                    symbol_tf = stream.split('@kline_')
                    if len(symbol_tf) != 2:
                        continue
                    symbol = symbol_tf[0].upper()
                    tf = symbol_tf[1]

                    k = data['k']
                    if not k['x']:  # 캔들 미완성 시 무시
                        continue
                    candle = {
                        "time": datetime.fromtimestamp(k['t'] / 1000),
                        "open": float(k['o']),
                        "high": float(k['h']),
                        "low": float(k['l']),
                        "close": float(k['c']),
                        "volume": float(k['v'])
                    }
                    candles[symbol][tf].append(candle)
                    send_discord_debug(f"[WS] {symbol}-{tf} 캔들 업데이트됨", "binance")                 

        except Exception as e:
            msg = f"❌ [BINANCE] WebSocket 연결 실패: {e}"
            print(msg)
            send_discord_debug(msg, "binance")

# 메인 실행 함수 (초기 로딩 + 실시간 연결)
async def start_data_feed():
    initialize_historical()
    await stream_live_candles()
