# main.py

import sys
import asyncio
from decimal import Decimal                # ★ 추가 import
from datetime import datetime, timezone
from dotenv import load_dotenv
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pandas as pd
from core.structure import detect_structure
from config.settings import (
    SYMBOLS,
    SYMBOLS_GATE,
    RR,
    SL_BUFFER,
    DEFAULT_LEVERAGE,
    ENABLE_GATE,          # ★ 추가
)
from core.data_feed import candles, initialize_historical, stream_live_candles
from core.iof import is_iof_entry
from core.position import PositionManager
from core.monitor import maybe_send_weekly_report
from core.ob import detect_ob
from exchange.router import get_open_position, update_stop_loss
from exchange.binance_api import place_order_with_tp_sl as binance_order_with_tp_sl
from exchange.binance_api import get_tick_size, calculate_quantity
from exchange.binance_api import (
    set_leverage,
    get_max_leverage,
    get_available_balance,
    get_open_position as binance_pos,   # ★ 복원
)
# Gate.io 연동은 ENABLE_GATE 가 True 일 때만 임포트
if ENABLE_GATE:
    from exchange.gate_sdk import (
        place_order_with_tp_sl as gate_order_with_tp_sl,
        get_open_position as gate_pos,
        set_leverage as gate_set_leverage,
        get_available_balance as gate_get_balance,
        get_tick_size as get_tick_size_gate,
        calculate_quantity as calculate_quantity_gate,
        to_gate_symbol as to_gate,        # ← 실제 함수명이 다르면 맞춰 주세요
    )
from notify.discord import send_discord_debug, send_discord_message

load_dotenv()
pm = PositionManager()

# ───────────────────────────── 헬퍼 ─────────────────────────────
async def handle_pair(symbol: str, meta: dict, htf_tf: str, ltf_tf: str):
    """
    symbol : Binance → BTCUSDT / Gate → BTC_USDT
    meta   : 최소 {"leverage": …}.  비어 있으면 DEFAULT_LEVERAGE 사용
    """
    leverage = meta.get("leverage", DEFAULT_LEVERAGE)
    # ───────── 중복 진입 방지 (내부 + 실시간) ─────────
    if pm.has_position(symbol):
        print(f"[SKIP] 내부 포지션 중복 방지 → {symbol}")
        return
    # 실시간 확인: Binance + Gate 모두 대응
    live_pos = get_open_position(symbol)
    if live_pos and abs(live_pos.get("entry", 0)) > 0:
        print(f"[SKIP] 실시간 포지션 확인됨 → {symbol}")
        return
    
    try:
        # ▸ candle dict 는 항상 Binance 포맷(BTCUSDT) 키 사용
        is_gate = "_USDT" in symbol
        base_sym = symbol.replace("_", "") if is_gate else symbol

        df_htf = candles.get(base_sym, {}).get(htf_tf)
        df_ltf = candles.get(base_sym, {}).get(ltf_tf)
        if df_htf is None or df_ltf is None or len(df_htf) < 30 or len(df_ltf) < 30:
            return

        # ▸ 심볼·타임프레임 메타데이터 주입
        htf = pd.DataFrame(df_htf)
        htf.attrs["symbol"] = base_sym.upper()
        htf.attrs["tf"]     = htf_tf

        ltf = pd.DataFrame(df_ltf)
        ltf.attrs["symbol"] = base_sym.upper()
        ltf.attrs["tf"]     = ltf_tf

        htf_struct = detect_structure(htf)
        if (
            htf_struct is None
            or "structure" not in htf_struct.columns
            or htf_struct["structure"].dropna().empty
        ):
            return

        tick_size = (
            Decimal(str(get_tick_size_gate(symbol)))
            if is_gate else
            get_tick_size(base_sym)
        )

        # ⬇️ htf 전체 DataFrame을 그대로 넘겨야 attrs 를 활용할 수 있음
        signal, direction = is_iof_entry(htf, ltf, tick_size)
        if not signal or direction is None:
            return

        entry = float(ltf["close"].iloc[-1])
        # Zone 기반 SL/TP 계산 (OB 사용)
        zone = None
        # 최근 OB 중 현재 방향과 일치하는 마지막 zone 선택
        for ob in reversed(detect_ob(ltf)):
            if ob["type"].lower() == direction:
                zone = ob
                break
        if zone:
            # buffer_value = SL_BUFFER 틱 * tick_size
            buffer_value = Decimal(str(SL_BUFFER)) * tick_size
            entry_dec = Decimal(str(entry))
            if direction == "long":
                zone_low = Decimal(str(zone["low"])).quantize(tick_size)
                sl_dec = (zone_low - buffer_value).quantize(tick_size)
                tp_dec = (entry_dec + (entry_dec - sl_dec) * Decimal(str(RR))).quantize(tick_size)
            else:
                zone_high = Decimal(str(zone["high"])).quantize(tick_size)
                sl_dec = (zone_high + buffer_value).quantize(tick_size)
                tp_dec = (entry_dec - (sl_dec - entry_dec) * Decimal(str(RR))).quantize(tick_size)
            sl, tp = float(sl_dec), float(tp_dec)
        else:
            sl, tp = calculate_sl_tp(entry, direction, SL_BUFFER, RR)

        order_ok = False
        if is_gate:
            balance = gate_get_balance()
            qty = calculate_quantity_gate(symbol, entry, balance, leverage)
            print(f"[GATE] 잔고={balance:.2f}, 수량={qty}")
            
            if qty <= 0:
                return
            order_ok = gate_order_with_tp_sl(
                symbol,
                "buy" if direction == "long" else "sell",
                qty, tp, sl, leverage
            )
        else:
            qty = calculate_quantity(symbol, entry, get_available_balance(), leverage)
            if qty <= 0:
                return
            order_ok = binance_order_with_tp_sl(
                symbol,
                "buy" if direction == "long" else "sell",
                qty, tp, sl            # <-- hedge 파라미터 제거
            )

        if order_ok:
            pm.enter(symbol, direction, entry, sl, tp)
            # ▶ 초기 SL 주문 (진입 근거 캔들 하단)
            update_stop_loss(symbol, direction, sl)
        else:
            print(f"[WARN] 주문 실패로 포지션 등록 건너뜀 | {symbol}")
            send_discord_debug(f"[WARN] 주문 실패 → 포지션 미등록 | {symbol}", "aggregated")
        pm.update_price(symbol, entry, ltf_df=ltf)      # MSS 보호선 갱신

    except Exception as e:
        print(f"[ERROR] {symbol} {htf_tf}/{ltf_tf} → {e}", "aggregated")
        #send_discord_debug(f"[ERROR] {symbol} {htf_tf}/{ltf_tf} → {e}", "aggregated")

def calculate_sl_tp(entry: float, direction: str, buffer: float, rr: float):
    if direction == 'long':
        sl = entry * (1 - buffer)
        tp = entry + (entry - sl) * rr
    else:
        sl = entry * (1 + buffer)
        tp = entry - (sl - entry) * rr
    return float(sl), float(tp)

def initialize():
    print("🚀 [INIT] 초기 세팅 시작")
    send_discord_message("🚀 [INIT] 초기 세팅 시작", "aggregated")
    initialize_historical()
    failed_positions = []
    failed_leverage = []
    for symbol, data in SYMBOLS.items():
        try:
            pos = binance_pos(symbol)
            if pos and 'entry' in pos and 'direction' in pos:
                sl, tp = calculate_sl_tp(pos['entry'], pos['direction'], SL_BUFFER, RR)
                pm.init_position(symbol, pos['direction'], pos['entry'], sl, tp)
            # 포지션이 없으면 Quiet 패스
        except Exception:
            failed_positions.append(symbol)
        try:
            max_lev = get_max_leverage(symbol)
            req_lev = data['leverage']
            applied_lev = min(req_lev, max_lev)
            set_leverage(symbol, applied_lev)
        except Exception as e:
            print(f"[WARN] 레버리지 설정 실패: {symbol} → {e}")
            failed_leverage.append(symbol)

    if failed_positions:
        warn_msg = f"⚠️ 포지션 조회 실패: {', '.join(failed_positions)}"
        print(f"[WARN] {warn_msg}")
        send_discord_debug(warn_msg, "aggregated")
    if failed_leverage:
        warn_msg = f"⚠️ 레버리지 설정 실패: {', '.join(failed_leverage)}"
        print(f"[WARN] {warn_msg}")
        send_discord_debug(warn_msg, "aggregated")
async def strategy_loop():
    print("📈 전략 루프 시작됨 (5초 간격)")
    send_discord_message("📈 전략 루프 시작됨 (5초 간격)", "aggregated")
    while True:
        # ───── Binance 스윙 1h→5m ─────
        for symbol, meta in SYMBOLS.items():
            await handle_pair(symbol, meta, "1h", "5m")

        # ───── Gate.io 단타 15m→1m (듀얼 모드 전용) ─────
        if ENABLE_GATE:
            for symbol in SYMBOLS_GATE:
                try:
                    gate_sym = to_gate(symbol)
                except ValueError as e:
                    print(f"[WARN] Gate 미지원 심볼 제외: {symbol} ({e})")
                    continue
                await handle_pair(gate_sym, {}, "15m", "1m")
# ──────────────────────────────────────────────────────────────

        await asyncio.sleep(5)

        # ───── 주간 리포트 (일요일 자정 UTC) ─────
        maybe_send_weekly_report(datetime.now(timezone.utc))

async def main():
    initialize()
    await asyncio.gather(
        stream_live_candles(),
        strategy_loop()
    )

def force_entry(symbol, side, qty_override=None):
    """
    임시·수동 진입(디버그)용 헬퍼  
    side == "buy"  ➜ long,  "sell" ➜ short
    TP·SL를 **진입 방향과 일치**하도록 1 % 고정
    """
    # 현재 마크가격 조회 (Gate·Binance 모두 지원)
    if symbol.endswith("_USDT"):
        if not ENABLE_GATE:
            print("❌ Gate.io 기능이 비활성화 상태입니다 (ENABLE_GATE=False)")
            return
        import requests, json, time, requests

        def gate_mark(s: str) -> float:
            """mark_price → 실패 시 ticker 로 Fallback"""
            url = f"https://fx-api.gateio.ws/api/v4/futures/usdt/mark_price/{s}"
            data = requests.get(url, timeout=3).json()
            if isinstance(data, dict) and "mark_price" in data:
                return float(data["mark_price"])

            # ─ fallback: /tickers (배열)
            tick = requests.get(
                "https://fx-api.gateio.ws/api/v4/futures/usdt/tickers",
                params={"contract": s},
                timeout=3,
            ).json()
            if tick and isinstance(tick, list):
                return float(tick[0]["last"])
            raise RuntimeError(f"Gate mark price fetch failed: {data}")

        price = gate_mark(symbol)
    else:
        import requests
        mk = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}").json()
        price = float(mk["markPrice"])
        
    # ───────── 수량 결정 ─────────
    leverage = DEFAULT_LEVERAGE

    if qty_override is not None:
        # 사용자가 --qty 로 직접 지정
        size = qty_override
    else:
        # 자동 산출
        if symbol.endswith("_USDT"):      # Gate 선물
            # Gate 잔고 조회 함수명 통일
            size = calculate_quantity_gate(symbol, price, gate_get_balance(), leverage)
        else:                             # Binance 선물
            set_leverage(symbol, leverage)      # 미리 적용
            size = calculate_quantity(symbol, price, get_available_balance(), leverage)

    if size <= 0:
        print("❌ 최소 주문 수량 미달 – 강제 진입 취소")
        return

    if side.lower() == "buy":      # long
        tp = price * 1.01          # +1 % 이익
        sl = price * 0.99          # −1 % 손절
    else:                          # short
        tp = price * 0.99          # −1 % 이익
        sl = price * 1.01          # +1 % 손절

    print(f"🚀 강제 진입 테스트: {symbol}, side={side}, size={size}, TP={tp}, SL={sl}")
    
    if symbol.endswith("_USDT"):          # Gate 선물
        # Gate 주문 함수는 gate_order_with_tp_sl 로 통일
        ok = gate_order_with_tp_sl(symbol, side, size, tp, sl, leverage)
    else:                                 # Binance 선물 심볼
        ok = binance_order_with_tp_sl(symbol, side, size, tp, sl)

    print("✅ 강제 진입 성공" if ok else "❌ 강제 진입 실패")


# entrypoint
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SMC-Trader launcher")
    parser.add_argument("--demo",  action="store_true",
                        help="강제 진입(debug)만 실행하고 종료")
    parser.add_argument("--side",  default="buy",
                        choices=["buy", "sell"], help="강제 진입 방향")
    parser.add_argument("--sym",   default="XRPUSDT",
                        help="거래 심볼")
    parser.add_argument("--qty",   type=float, default=None,
                        help="테스트용 강제 수량(지정 시 자동 계산 건너뜀)")
    args = parser.parse_args()

    if args.demo:
        # ▸ 단발성 진입 테스트만 수행
        force_entry(args.sym, args.side, args.qty)
    else:
        # ▸ 전체 전략 루프 실행
        asyncio.run(main())