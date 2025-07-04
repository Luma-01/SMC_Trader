# core/data_feed.py

import aiohttp
import asyncio
import requests
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
# settings 에서 Gate 사용 여부도 같이 가져옴
from config.settings import (
    SYMBOLS, TIMEFRAMES, CANDLE_LIMIT, ENABLE_GATE,
    LTF_TF,          # ex) "1h"
    HTF_TF,          # ex) "1d"
)
import json                        # 🌟 Gate WS 메시지 파싱용
from notify.discord import send_discord_debug
import pandas as pd
import threading

# ▸ main.py 에서 생성한 singleton pm 가져오기(순환참조 방지용 late import)
pm = None                            # ↙ 나중에 set_pm() 으로 주입

LIVE_STREAMS   : set[str] = set()        # 현재 열려있는 심볼 스트림
STREAM_THREADS : dict[str, threading.Thread] = {}

# ---------------------------------------------------------------------------
# ⛳  Symbol‑mapping helper (📌 "단 한 곳"에만 유지하기)
#
#  · 외부 API → 내부 사용   : to_canon("BTCUSDT") == "BTC_USDT"
#  · 내부 키   → REST/WS용 : to_binance("BTC_USDT") == "BTCUSDT"
#
#  Canonical key = settings.SYMBOLS 의 키와 동일한 형태로 통일한다.
# ---------------------------------------------------------------------------


def to_canon(sym: str) -> str:
    """Binance 스타일(sym="BTCUSDT") →  settings.SYMBOLS 키("BTC_USDT")"""
    if sym.endswith("USDT") and not sym.endswith("_USDT"):
        candidate = sym.replace("USDT", "_USDT")
        return candidate if candidate in SYMBOLS else sym
    return sym


def to_binance(sym: str) -> str:
    """Canonical("BTC_USDT") → REST/WS 에 쓰는 "BTCUSDT"""
    return sym.replace("_", "")
# 간단한 게이트 심볼 판별 한 줄짜리
def is_gate_sym(sym: str) -> bool:
    return sym.endswith("_USDT")

# ▶ settings 안 TIMEFRAMES 전체를 그대로 쓰고,
#   그중 LTF_TF/HTF_TF 를 기준 타임프레임으로 사용
TIMEFRAMES_BINANCE = TIMEFRAMES
LTF = LTF_TF
HTF = HTF_TF

def _ws_worker(symbol: str):
    """
    새 심볼 전용 단일-WS. 1m·5m 등 모든 TIMEFRAMES 를 구독한다.
    메인 루프와 동일한 candle append + pm.update_price 호출 로직 재사용.
    """
    global pm               # 스레드 내에서 최신 pm 참조
    pairs = [f"{to_binance(symbol).lower()}@kline_{tf}"  # ← Binance 형식으로 변환
             for tf in TIMEFRAMES_BINANCE]
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
                    # ⭐ 포지션 업데이트는 **설정된 LTF_TF** 로만
                    if tf == LTF and pm.has_position(symbol.upper()):
                        ltf_df = pd.DataFrame(candles[symbol.upper()][LTF])
                        pm.update_price(symbol.upper(), candle["close"],
                                        ltf_df=ltf_df)

    asyncio.run(_runner())               # 별도 스레드-> 독립 event-loop

def ensure_stream(symbol: str):
    """
    `pm.enter()` 에서 호출. 이미 스트림이 있으면 no-op,
    아니면 **백그라운드 스레드**로 `_ws_worker` 시작.
    """
    symbol = to_binance(symbol)          # 항상 Binance 포맷으로 넘김
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
# ▶ USDT-M Futures (FAPI) 엔드포인트로 교체
BINANCE_REST_URL = "https://fapi.binance.com"
BINANCE_WS_URL   = "wss://fstream.binance.com/stream?streams="
# Gate Futures v4 USDT-settled WS
GATE_WS_URL      = "wss://fx-ws.gateio.ws/v4/ws/usdt"

# ────────────────────────────────────────────────────────────────
#  ✨ 공통 Runner : WS 코루틴이 죽어도 알아서 재접속
#     • CancelledError → 그대로 전파(상위 gather 가 정상 종료시킴)
#     • 기타 예외      → 로그 찍고 back-off 재시도
# ────────────────────────────────────────────────────────────────
import traceback, math

async def _run_forever(coro_factory, tag: str):
    backoff = 1.0                            # seconds
    while True:
        try:
            await coro_factory()             # 실제 stream 코루틴 실행
        except asyncio.CancelledError:
            raise                            # ← graceful shutdown
        except Exception as e:
            print(f"[WS][{tag}] crashed → {e!r}")
            traceback.print_exc()
            print(f"[WS][{tag}] reconnect in {backoff:.0f}s …")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)   # 1 → 2 → 4 … 최대 60
        else:
            # 정상 return 은 비정상 상황 → 곧바로 재시작
            print(f"[WS][{tag}] returned unexpectedly – restarting")

# 캔들 저장소: {symbol: {timeframe: deque}}
candles = defaultdict(lambda: defaultdict(lambda: deque(maxlen=CANDLE_LIMIT)))

# 1. 과거 캔들 로딩 (REST)
# ─────────────────────────── Binance 전용 ───────────────────────────
def load_historical_candles_binance(
    symbol: str, interval: str, limit: int = CANDLE_LIMIT
):
    # Binance REST 는 'BTCUSDT' 형태만 허용
    url = f"{BINANCE_REST_URL}/api/v3/klines"
    params = {
        "symbol": to_binance(symbol),        # canonical → Binance
        "interval": interval,
        "limit": limit
    }
    response = requests.get(url, params=params, timeout=5)
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

# ─────────────────────────── Gate 전용 ──────────────────────────────
def load_historical_candles_gate(
    contract: str, interval: str, limit: int = CANDLE_LIMIT
):
    """
    Gate v4  선물 캔들 엔드포인트  
      GET /futures/usdt/candlesticks?contract=BTC_USDT&interval=1m&limit=150
    """
    url = "https://fx-api.gateio.ws/api/v4/futures/usdt/candlesticks"
    # ---- 공통 헤더 ------------------------------------------------
    _HDR = {
        "User-Agent": "Mozilla/5.0 (SMC-Trader)",
        "Accept":     "application/json",
    }

    step_sec = {
        "1m": 60, "5m": 300, "15m": 900,
        "1h": 3600, "4h": 14400, "1d": 86400
    }[interval]
    now_sec   = int(datetime.now(timezone.utc).timestamp())
    from_sec  = now_sec - step_sec * limit

    # ── ① 첫 번째 시도: limit만 ─────────────────────────────
    params = {
        "contract": contract,
        "interval": interval,
        "limit":    limit,
    }
    resp  = requests.get(url, params=params, headers=_HDR, timeout=5)
    try:
        data = resp.json()
    except Exception:
        data = None

    # 빈 배열이면 ② from/to 재시도 (limit 제거) ───────────────
    if isinstance(data, list) and not data:
        params = {
            "contract": contract,
            "interval": interval,
            "from":     from_sec,
            "to":       now_sec,     # ← limit 없이 from-to 범위 지정
        }
        resp  = requests.get(url, params=params, headers=_HDR, timeout=5)
        try:
            data = resp.json()
        except Exception:
            data = None


    # ---- 실패 처리 -----------------------------------------------
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} – {resp.text[:200]}...")
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("빈 응답")
    # ----------------------------------------------------------------

    out = []
    for d in data:
        # v4 API 응답이 `list` ↔ `dict` 모두 섞여 들어올 수 있음
        if isinstance(d, list):                # ▶ 전통적인 배열
            ts, o, h, l, c, v = d[:6]
        elif isinstance(d, dict):              # ▶ 키-값 포맷
            ts = int(d.get("t") or d.get("timestamp"))
            o  = d.get("o") or d["open"]
            h  = d.get("h") or d["high"]
            l  = d.get("l") or d["low"]
            c  = d.get("c") or d["close"]
            v  = d.get("v") or d.get("volume") or 0   # ← volume 누락 시 0 으로
        else:                                  # 예외-케이스 방어
            continue

        out.append(
            {
                "time":   datetime.fromtimestamp(int(ts)),
                "open":   float(o),
                "high":   float(h),
                "low":    float(l),
                "close":  float(c),
                "volume": float(v),
            }
        )
    return out

def initialize_historical():
    # ✔︎ 거래소별 집계
    ok_bi = ok_ga = 0
    fail_bi: list[str] = []
    fail_ga: list[str] = []
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                if ENABLE_GATE and symbol.endswith("_USDT"):
                    data = load_historical_candles_gate(symbol, tf)
                    ok_ga += 1
                else:
                    data = load_historical_candles_binance(symbol.replace("_", ""), tf)
                    ok_bi += 1

                candles[symbol][tf].extend(data)
            except Exception as e:                        # ← 실패 처리
                tag = f"{symbol}-{tf} ({repr(e)})"        # 내용 전체 보이도록
                if symbol.endswith("_USDT"):
                    fail_ga.append(tag)
                else:
                    fail_bi.append(tag)

                # 상세 원인을 콘솔·디스코드에 즉시 출력
                print(f"[HIST] FAIL → {tag}")
                send_discord_debug(f"❌ 캔들 로딩 실패: {tag}", "aggregated")
    # ───────── 결과 요약 ─────────
    summary = [
        "📊 [HIST] 과거 캔들 로딩 결과",
        f" ├─ Binance : ✅ 성공 {ok_bi} / ❌ 실패 {len(fail_bi)}",
        f" └─ Gate    : ✅ 성공 {ok_ga} / ❌ 실패 {len(fail_ga)}",
    ]
    if fail_bi:
        summary.append(f"    • Binance 실패 → {', '.join(fail_bi)}")
    if fail_ga:
        summary.append(f"    • Gate    실패 → {', '.join(fail_ga)}")

    msg = "\n".join(summary)
    print(msg)
    send_discord_debug(msg, "aggregated")

# 2-A. Binance 실시간 WebSocket
async def stream_live_candles_binance():
    stream_pairs = [
        f"{to_binance(symbol).lower()}@kline_{tf}"
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
                        if pm and tf == LTF and pm.has_position(symbol):
                            ltf_df = pd.DataFrame(candles[symbol][LTF])
                            # ─ 보호선용 상위 TF(HTF_TF) DataFrame
                            htf_df = (
                                pd.DataFrame(candles[symbol][HTF])
                                if candles[symbol][HTF] else None
                            )
                            # 오타 수정: htf_df
                            pm.update_price(
                                symbol,
                                candle["close"],
                                ltf_df = ltf_df,
                                htf_df = htf_df,
                            )
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

                # ▶️  (1) 채널·이벤트 필터
                if data.get("channel") != "futures.candlesticks" or data.get("event") != "update":
                    continue

                # ▶️  (2) payload 안전 체크
                res = data.get("result", [])
                if not (isinstance(res, list) and len(res) == 3):
                    # heartbeat/ping 등  형식이 다른 패킷은 스킵
                    continue

                # payload: [tf, "BTC_USDT", [ts, o, h, l, c, v]]
                tf, sym, k = res
                candle = {
                    "time":   datetime.fromtimestamp(k[0] / 1000),
                    "open":   float(k[1]),
                    "high":   float(k[2]),
                    "low":    float(k[3]),
                    "close":  float(k[4]),
                    "volume": float(k[5])
                }
                candles[sym][tf].append(candle)
                if pm and tf == LTF and pm.has_position(sym):
                    ltf_df = pd.DataFrame(candles[sym][LTF])
                    pm.update_price(sym, candle["close"], ltf_df=ltf_df)

# 3. 초기 로딩 + WS 병렬 실행
#    ※ initialize_historical() 는 main.initialize() 에서
#      이미 한 번 호출되므로 **여기서는 생략**합니다.

# ------------------------------------------------------------
# 🔄  _run_forever 래퍼 (앞서 추가한 헬퍼) 를 이용해
#     스트림이 죽어도 자동 재연결하도록 감싼 진짜 “export” 함수
# ------------------------------------------------------------

async def start_data_feed() -> None:
    """
    외부(main.py)에서 import 하는 진입점.
    두 거래소 WS 스트림을 각각 무한 재시도 러너로 실행한다.
    """
    # ───────── 실행할 스트림 목록 동적 구성 ─────────
    tasks = [
        _run_forever(stream_live_candles_binance, "BINANCE")
    ]

    # Gate 스트림은 ENABLE_GATE 일 때만 추가
    if ENABLE_GATE:
        tasks.append(
            _run_forever(stream_live_candles_gate, "GATE")
        )
    else:
        print("[INFO] Gate WS disabled (ENABLE_GATE=False)")

    # 병렬 실행
    await asyncio.gather(*tasks)
