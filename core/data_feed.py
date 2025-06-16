# core/data_feed.py

import aiohttp
import asyncio
import requests
from collections import defaultdict, deque
from datetime import datetime
from config.settings import SYMBOLS, TIMEFRAMES, CANDLE_LIMIT, ENABLE_GATE
import json                        # 🌟 Gate WS 메시지 파싱용
from notify.discord import send_discord_debug
import pandas as pd
import threading

# ▸ main.py 에서 생성한 singleton pm 가져오기(순환참조 방지용 late import)
pm = None                            # ↙ 나중에 set_pm() 으로 주입

LIVE_STREAMS   : set[str] = set()        # 현재 열려있는 심볼 스트림
STREAM_THREADS : dict[str, threading.Thread] = {}

TIMEFRAMES_BINANCE = TIMEFRAMES          # 1m · 5m · 15m …

def _ws_worker(symbol: str):
    """
    새 심볼 전용 단일-WS. 1m·5m 등 모든 TIMEFRAMES 를 구독한다.
    메인 루프와 동일한 candle append + pm.update_price 호출 로직 재사용.
    """
    global pm               # 스레드 내에서 최신 pm 참조
    pairs = [f"{symbol.lower()}@kline_{tf}" for tf in TIMEFRAMES_BINANCE]
    url   = BINANCE_WS_URL + "/".join(pairs)

    async def _runner():
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url) as ws:
                async for msg in ws:
                    raw   = msg.json()
                    data  = raw["data"]
                    tf    = raw["stream"].split("@kline_")[1]
                    k     = data["k"]
                    if not k["x"]:
                        continue
                    candle = {
                        "time":   datetime.fromtimestamp(k["t"] / 1000),
                        "open":   float(k["o"]),
                        "high":   float(k["h"]),
                        "low":    float(k["l"]),
                        "close":  float(k["c"]),
                        "volume": float(k["v"]),
                    }
                    candles[symbol.upper()][tf].append(candle)
                    if tf == "1m" and pm.has_position(symbol.upper()):
                        ltf_df = pd.DataFrame(candles[symbol.upper()][tf])
                        pm.update_price(symbol.upper(), candle["close"],
                                        ltf_df=ltf_df)

    asyncio.run(_runner())               # 별도 스레드-> 독립 event-loop

def ensure_stream(symbol: str):
    """
    `pm.enter()` 에서 호출. 이미 스트림이 있으면 no-op,
    아니면 **백그라운드 스레드**로 `_ws_worker` 시작.
    """
    symbol = symbol.replace("_", "")     # Gate 심볼 대비
    if symbol in LIVE_STREAMS:
        return
    LIVE_STREAMS.add(symbol)
    th = threading.Thread(target=_ws_worker, args=(symbol,), daemon=True)
    STREAM_THREADS[symbol] = th
    th.start()

# PositionManager 인스턴스를 주입하기 위한 헬퍼
def set_pm(manager):
    """
    main.py 에서 생성한 PositionManager 를 늦게 주입한다.
    순환 import 문제를 피하기 위한 dependency-injection 훅.
    """
    global pm
    pm = manager


# ----------------------------------------------- REST / WS End-points
BINANCE_REST_URL = "https://api.binance.com"
BINANCE_WS_URL   = "wss://stream.binance.com:9443/stream?streams="
# Gate Futures v4 USDT-settled WS
GATE_WS_URL      = "wss://fx-ws.gateio.ws/v4/ws/usdt"


# 캔들 저장소: {symbol: {timeframe: deque}}
candles = defaultdict(lambda: defaultdict(lambda: deque(maxlen=CANDLE_LIMIT)))

# 1. 과거 캔들 로딩 (REST)
def load_historical_candles(symbol: str, interval: str, limit: int = CANDLE_LIMIT):
    # Binance REST 는 'BTCUSDT' 형태만 허용
    url = f"{BINANCE_REST_URL}/api/v3/klines"
    params = {
        "symbol": symbol.replace("_", ""),   # 'BTC_USDT' → 'BTCUSDT'
        "interval": interval,
        "limit": limit
    }
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
                # REST 호출용(밑줄 제거) ↔ 저장용(원본) 분리
                data = load_historical_candles(symbol.replace("_", ""), tf)
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

# 2-A. Binance 실시간 WebSocket
async def stream_live_candles_binance():
    stream_pairs = [
        f"{symbol.replace('_', '').lower()}@kline_{tf}"
        for symbol in SYMBOLS
        for tf in TIMEFRAMES
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
                    stream_symbol = symbol_tf[0].upper()           # 'BTCUSDT'
                    gate_symbol   = stream_symbol.replace("USDT", "_USDT")
                    tf = symbol_tf[1]
                    
                    # Gate 모드에선 저장 키를 'BTC_USDT' 로 맞춘다
                    symbol = gate_symbol if gate_symbol in SYMBOLS else stream_symbol
                    symbol = symbol.upper()

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
                    if symbol in SYMBOLS:
                        candles[symbol][tf].append(candle)
                        # ───── 실시간 포지션 가격·SL 갱신 ─────
                        if pm and tf == "1m" and pm.has_position(symbol):
                            ltf_df = pd.DataFrame(candles[symbol][tf])
                            pm.update_price(symbol, candle["close"], ltf_df=ltf_df)
                    #send_discord_debug(f"[WS] {symbol}-{tf} 캔들 업데이트됨", "binance")                 

        except Exception as e:
            msg = f"❌ [BINANCE] WebSocket 연결 실패: {e}"
            print(msg)
            send_discord_debug(msg, "binance")
    
# 2-B. Gate 실시간 WebSocket  (futures.candlesticks)
async def stream_live_candles_gate():
    if not ENABLE_GATE:
        return

    gate_symbols = [s for s in SYMBOLS if s.endswith("_USDT")]
    if not gate_symbols:
        return

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(GATE_WS_URL) as ws:
            # 구독 메시지 일괄 전송
            for sym in gate_symbols:
                for tf in TIMEFRAMES:
                    sub = {
                        "time": 0,
                        "channel": "futures.candlesticks",
                        "event": "subscribe",
                        "payload": [tf, sym],
                    }
                    await ws.send_json(sub)
            print("✅ [WS] Gate WebSocket 연결·구독 성공!")

            async for msg in ws:
                data = json.loads(msg.data)
                if data.get("channel") != "futures.candlesticks" or data.get("event") != "update":
                    continue

                # payload: [tf, "BTC_USDT", [ts, o, h, l, c, v]]
                tf, sym, k = data["result"]
                candle = {
                    "time":   datetime.fromtimestamp(k[0] / 1000),
                    "open":   float(k[1]),
                    "high":   float(k[2]),
                    "low":    float(k[3]),
                    "close":  float(k[4]),
                    "volume": float(k[5])
                }
                candles[sym][tf].append(candle)
                if pm and tf == "1m" and pm.has_position(sym):
                    ltf_df = pd.DataFrame(candles[sym][tf])
                    pm.update_price(sym, candle["close"], ltf_df=ltf_df)

# 3. 초기 로딩 + WS 병렬 실행
async def start_data_feed():
    initialize_historical()
    await asyncio.gather(
        stream_live_candles_binance(),
        stream_live_candles_gate()
    )
