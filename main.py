import asyncio
import pandas as pd
from config.settings import SYMBOLS, RR, SL_BUFFER
from core.data_feed import candles, initialize_historical, stream_live_candles
from core.iof import is_iof_entry
from core.position import PositionManager
from exchange.binance_api import place_order as binance_order, get_open_position as binance_pos, set_leverage
from exchange.gate_sdk import place_order as gate_order, get_open_position as gate_pos
from notify.discord import send_discord_alert

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
    print("[INIT] 초기 세팅 중...")
    initialize_historical()
    failed_positions = []
    failed_leverage = []
    for symbol, data in SYMBOLS.items():
        try:
            pos = binance_pos(symbol)
            if pos:
                sl, tp = calculate_sl_tp(pos['entry'], pos['direction'], SL_BUFFER, RR)
                pm.init_position(symbol, pos['direction'], pos['entry'], sl, tp)
        except Exception:
            failed_positions.append(symbol)
        try:
            set_leverage(symbol, data['leverage'])
        except Exception:
            failed_leverage.append(symbol)

    if failed_positions:
        print(f"[WARN] 포지션 조회 실패 심볼: {', '.join(failed_positions)}")
    if failed_leverage:
        print(f"[WARN] 레버리지 설정 실패 심볼: {', '.join(failed_leverage)}")

async def strategy_loop():
    while True:
        for symbol in SYMBOLS:
            try:
                df_htf = candles[symbol]['1h']
                df_ltf = candles[symbol]['5m']
                if len(df_htf) < 30 or len(df_ltf) < 30:
                    continue

                htf = pd.DataFrame(df_htf)
                ltf = pd.DataFrame(df_ltf)

                signal, direction = is_iof_entry(htf, ltf)

                if signal and not pm.has_position(symbol):
                    entry = ltf['close'].iloc[-1]
                    sl, tp = calculate_sl_tp(entry, direction, SL_BUFFER, RR)

                    qty = SYMBOLS[symbol]['minQty']
                    lev = SYMBOLS[symbol]['leverage']

                    # 진입
                    binance_order(symbol, 'buy' if direction == 'long' else 'sell', qty)
                    gate_order(symbol.replace("USDT", "_USDT"), 'buy' if direction == 'long' else 'sell', qty, lev)

                    # 포지션 등록
                    pm.enter(symbol, direction, entry, sl, tp)

                # 실시간 구조 업데이트 + MSS 보호선 체크
                current_price = ltf['close'].iloc[-1]
                pm.update_price(symbol, current_price, ltf_df=ltf)

            except Exception as e:
                send_discord_alert(f"[ERROR] {symbol} 전략 오류: {e}")

        await asyncio.sleep(5)

async def main():
    initialize()
    await asyncio.gather(
        stream_live_candles(),
        strategy_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())