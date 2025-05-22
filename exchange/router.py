# exchange/router.py

from exchange.binance_api import update_stop_loss_order as binance_sl
# Gate 전용 함수 import
from exchange.gate_sdk import (
    update_stop_loss_order as gate_sl,
    normalize_contract_symbol as to_gate,
)
# Gate 심볼 집합(BTC_USDT 형식) 생성
from config.settings import SYMBOLS_GATE
GATE_SET = {to_gate(sym) for sym in SYMBOLS_GATE}

def update_stop_loss(symbol: str, direction: str, stop_price: float):
    """
    symbol 예시
      - Binance : BTCUSDT
      - Gate    : BTC_USDT  ← 이미 변환된 값
    """
    if symbol in GATE_SET:       # Gate 심볼이면
        return gate_sl(symbol, direction, stop_price)
    return binance_sl(symbol, direction, stop_price)
    
def cancel_order(symbol: str, order_id: int):
    if "_USDT" in symbol:
        # Gate는 SL 주문 ID가 없으므로 전체 포지션 종료로 대체
        from exchange.gate_sdk import close_position
        return close_position(symbol)
    else:
        from exchange.binance_api import cancel_order as binance_cancel_order
        return binance_cancel_order(symbol, order_id)