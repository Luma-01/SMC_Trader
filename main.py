# main.py

import sys
import asyncio

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pandas as pd
from core.structure import detect_structure
from config.settings import SYMBOLS, RR, SL_BUFFER
from core.data_feed import candles, initialize_historical, stream_live_candles
from core.iof import is_iof_entry
from core.position import PositionManager
from exchange.binance_api import place_order as binance_order, get_open_position as binance_pos, set_leverage
from exchange.binance_api import get_max_leverage, get_available_balance, get_quantity_precision
from exchange.gate_sdk import place_order_with_tp_sl as gate_order, get_open_position as gate_pos
from exchange.gate_sdk import get_available_balance as gate_balance, get_quantity_precision as gate_precision
from notify.discord import send_discord_debug, send_discord_message


pm = PositionManager()

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
        for symbol in SYMBOLS:
            try:
                df_htf = candles.get(symbol, {}).get('1h', None)
                df_ltf = candles.get(symbol, {}).get('5m', None)

                if df_htf is None or df_ltf is None:
                    print(f"[{symbol}] ❌ 캔들 데이터 자체 None (htf/ltf)")
                    send_discord_debug(f"[{symbol}] ❌ 캔들 데이터 자체 None (htf/ltf)", "aggregated")
                    continue

                if not df_htf or not df_ltf:
                    print(f"[SKIP] {symbol} 캔들 데이터 부족 (htf/ltf)")
                    send_discord_debug(f"[SKIP] {symbol} 캔들 데이터 부족 (htf/ltf)", "aggregated")
                    continue
                if len(df_htf) < 30 or len(df_ltf) < 30:
                    continue

                htf = pd.DataFrame(df_htf)
                htf.attrs["symbol"] = symbol
                ltf = pd.DataFrame(df_ltf)
                ltf.attrs["symbol"] = symbol

                #print(f"[DEBUG] {symbol} HTF 마지막 5개 캔들:\n{htf.tail(5)}")

                htf_struct = detect_structure(htf)
                if 'structure' not in htf_struct.columns:
                    print(f"[{symbol}] ❌ 구조 컬럼 없음")
                    send_discord_debug(f"[{symbol}] ❌ 구조 컬럼 없음", "aggregated")
                    continue

                try:
                    result = is_iof_entry(htf_struct, ltf)
                    if not isinstance(result, tuple) or len(result) != 2:
                        print(f"[{symbol}] ❌ IOF 결과 형식 오류: {result}")
                        send_discord_debug(f"[{symbol}] ❌ IOF 결과 형식 오류: {result}", "aggregated")
                        continue
                    signal, direction = result
                except Exception as e:
                    print(f"[{symbol}] ❌ IOF 함수 실행 중 오류: {e}")
                    send_discord_debug(f"[{symbol}] ❌ IOF 함수 실행 중 오류: {e}", "aggregated")
                    continue

                if not signal or not direction:
                    print(f"[{symbol}] 🚫 IOF 조건 불충족 → signal={signal}, direction={direction}")
                    # send_discord_debug(f"[{symbol}] 🚫 IOF 조건 불충족 → signal={signal}, direction={direction}", "aggregated")
                    continue

                if ltf.empty or 'close' not in ltf.columns or ltf['close'].dropna().empty:
                    print(f"[{symbol}] ❌ 진입 시도 실패: LTF 종가 없음")
                    send_discord_debug(f"[{symbol}] ❌ 진입 시도 실패: LTF 종가 없음", "aggregated")
                    continue

                if not pm.has_position(symbol):
                    entry = ltf['close'].dropna().iloc[-1]
                    sl, tp = calculate_sl_tp(entry, direction, SL_BUFFER, RR)

                    # Binance 잔고 기반 진입 수량 계산
                    bnb_balance = get_available_balance()
                    bnb_risk_usdt = bnb_balance * 0.3
                    bnb_qty_precision = get_quantity_precision(symbol)
                    bnb_qty = round(bnb_risk_usdt / entry, bnb_qty_precision)
                    if bnb_qty <= 0:
                        print(f"[{symbol}] ❌ Binance 진입 실패: 계산된 수량이 0 이하 (balance={bnb_balance}, qty={bnb_qty})")
                        continue

                    # Gate 잔고 기반 진입 수량 계산
                    from exchange.gate_sdk import get_available_balance as gate_balance, get_quantity_precision as gate_precision
                    gate_sym = symbol.replace("USDT", "_USDT")
                    gate_balance_usdt = gate_balance()
                    gate_risk_usdt = gate_balance_usdt * 0.3
                    gate_qty_precision = gate_precision(gate_sym)
                    gate_qty = round(gate_risk_usdt / entry, gate_qty_precision)
                    if gate_qty <= 0:
                        print(f"[{symbol}] ❌ Gate 진입 실패: 계산된 수량이 0 이하 (balance={gate_balance_usdt}, qty={gate_qty})")
                        continue

                    lev = SYMBOLS[symbol]['leverage']
                    binance_order(symbol, 'buy' if direction == 'long' else 'sell', bnb_qty)
                    gate_order(gate_sym, 'buy' if direction == 'long' else 'sell', gate_qty, tp, sl, lev)

                    # 포지션 등록
                    pm.enter(symbol, direction, entry, sl, tp)

                # 실시간 구조 업데이트 + MSS 보호선 체크
                current_price = ltf['close'].iloc[-1]
                pm.update_price(symbol, current_price, ltf_df=ltf)

            except Exception as e:
                error_msg = f"❌ [ERROR] {symbol} 전략 오류: {e}"
                print(error_msg)
                send_discord_debug(error_msg, "aggregated")

        await asyncio.sleep(5)

async def main():
    initialize()
    await asyncio.gather(
        stream_live_candles(),
        strategy_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())