# main.py

import sys
import asyncio
from decimal import Decimal                # ★ 추가 import
from datetime import datetime, timezone
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pandas as pd
from core.structure import detect_structure
from config.settings import (
    SYMBOLS,
    SYMBOLS_GATE,          # ★ 추가
    RR,
    SL_BUFFER,
    DEFAULT_LEVERAGE,      # ★ 추가
)
from core.data_feed import candles, initialize_historical, stream_live_candles
from core.iof import is_iof_entry
from core.position import PositionManager
from core.monitor import maybe_send_weekly_report
from exchange.binance_api import place_order_with_tp_sl as binance_order_with_tp_sl
from exchange.binance_api import get_tick_size, calculate_quantity
from exchange.binance_api import (
    set_leverage,
    get_max_leverage,
    get_available_balance,
    get_open_position as binance_pos,   # ★ 복원
)
from exchange.gate_sdk import (
    place_order_with_tp_sl as gate_order,
    get_available_balance as gate_balance,
    calculate_quantity_gate,
    get_tick_size_gate,
    normalize_contract_symbol as to_gate,      # ★ 이미 추가
)
from notify.discord import send_discord_debug, send_discord_message

pm = PositionManager()

# ───────────────────────────── 헬퍼 ─────────────────────────────
async def handle_pair(symbol: str, meta: dict, htf_tf: str, ltf_tf: str):
    """
    symbol : Binance → BTCUSDT / Gate → BTC_USDT
    meta   : 최소 {"leverage": …}.  비어 있으면 DEFAULT_LEVERAGE 사용
    """
    leverage = meta.get("leverage", DEFAULT_LEVERAGE)

    try:
        # ▸ candle dict 는 항상 Binance 포맷(BTCUSDT) 키 사용
        is_gate = "_USDT" in symbol
        base_sym = symbol.replace("_", "") if is_gate else symbol

        df_htf = candles.get(base_sym, {}).get(htf_tf)
        df_ltf = candles.get(base_sym, {}).get(ltf_tf)
        if df_htf is None or df_ltf is None or len(df_htf) < 30 or len(df_ltf) < 30:
            return

        htf = pd.DataFrame(df_htf); htf.attrs["tf"] = htf_tf
        ltf = pd.DataFrame(df_ltf)

        htf_struct = detect_structure(htf)
        if (
            htf_struct is None
            or "structure" not in htf_struct.columns
            or htf_struct["structure"].dropna().empty
        ):
            return

        if is_gate:
            tick_size = Decimal(str(get_tick_size_gate(symbol)))
        else:
            tick_size = get_tick_size(base_sym)
        signal, direction = is_iof_entry(htf_struct, ltf, tick_size)
        if not signal or direction is None:
            return

        entry = ltf["close"].iloc[-1]
        sl, tp = calculate_sl_tp(entry, direction, SL_BUFFER, RR)

        order_ok = False
        if is_gate:
            order_ok = gate_order(
                symbol,
                "buy" if direction == "long" else "sell",
                calculate_quantity_gate(symbol, entry, gate_balance(), leverage),
                tp, sl, leverage
            )
        else:
            order_ok = binance_order_with_tp_sl(
                symbol,
                "buy" if direction == "long" else "sell",
                calculate_quantity(symbol, entry, get_available_balance(), leverage),
                tp, sl,
                hedge=False
            )

        if order_ok:
            pm.enter(symbol, direction, entry, sl, tp)
        else:
            print(f"[WARN] 주문 실패로 포지션 등록 건너뜀 | {symbol}")
            send_discord_debug(f"[WARN] 주문 실패 → 포지션 미등록 | {symbol}", "aggregated")
        pm.update_price(symbol, entry, ltf_df=ltf)      # MSS 보호선 갱신

    except Exception as e:
        send_discord_debug(f"[ERROR] {symbol} {htf_tf}/{ltf_tf} → {e}", "aggregated")

def calculate_sl_tp(entry: float, direction: str, buffer: float, rr: float):
    if direction == 'long':
        sl = entry * (1 - buffer)
        tp = entry + (entry - sl) * rr
    else:
        sl = entry * (1 + buffer)
        tp = entry - (sl - entry) * rr
    return sl, tp

def initialize():
    print("🚀 [INIT] 초기 세팅 시작")
    send_discord_message("🚀 [INIT] 초기 세팅 시작", "aggregated")
    initialize_historical()
    failed_positions = []
    failed_leverage = []
    for symbol, data in SYMBOLS.items():
        try:
            pos = binance_pos(symbol)
            if isinstance(pos, dict) and 'entry' in pos and 'direction' in pos:
                sl, tp = calculate_sl_tp(pos['entry'], pos['direction'], SL_BUFFER, RR)
                pm.init_position(symbol, pos['direction'], pos['entry'], sl, tp)
            else:
                failed_positions.append(symbol)
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

        # ───── Gate.io 단타 15m→1m ─────
        for symbol in SYMBOLS_GATE:
            # candles·tick_size 조회는 BTCUSDT, 주문은 BTC_USDT
            await handle_pair(to_gate(symbol), {}, "15m", "1m")
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

def force_entry(symbol, side):
    """
    임시·수동 진입(디버그)용 헬퍼  
    side == "buy"  ➜ long,  "sell" ➜ short
    TP·SL를 **진입 방향과 일치**하도록 1 % 고정
    """
    # 현재 마크가격 조회 (Gate·Binance 모두 지원)
    if symbol.endswith("_USDT"):
        import requests, json
        mk = requests.get(f"https://fx-api.gateio.ws/api/v4/futures/usdt/mark_price/{symbol}").json()
        price = float(mk["mark_price"])
    else:
        import requests
        mk = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}").json()
        price = float(mk["markPrice"])
        
    size  = 1      # 테스트 수량

    if side.lower() == "buy":      # long
        tp = price * 1.01          # +1 % 이익
        sl = price * 0.99          # −1 % 손절
    else:                          # short
        tp = price * 0.99          # −1 % 이익
        sl = price * 1.01          # +1 % 손절

    print(f"🚀 강제 진입 테스트: {symbol}, side={side}, size={size}, TP={tp}, SL={sl}")
    
    # Gate 테스트용 강제 진입
    gate_sym = to_gate(symbol)
    if gate_order(gate_sym, side, size, tp, sl, DEFAULT_LEVERAGE):
        print("✅ 강제 진입 성공")
    else:
        print("❌ 강제 진입 실패")


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
    args = parser.parse_args()

    if args.demo:
        # ▸ 단발성 진입 테스트만 수행
        force_entry(args.sym, args.side)
    else:
        # ▸ 전체 전략 루프 실행
        asyncio.run(main())