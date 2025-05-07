# exchange/router.py

from exchange.binance_api import update_stop_loss_order as binance_sl
from exchange.gate_sdk import update_stop_loss_order as gate_sl

def update_stop_loss(symbol: str, direction: str, stop_price: float):
    if "_USDT" in symbol:  # Gate
        return gate_sl(symbol, direction, stop_price)
    else:  # Binance
        return binance_sl(symbol, direction, stop_price)
    
def cancel_order(symbol: str, order_id: int):
    if "_USDT" in symbol:
        # Gate는 SL 주문 ID가 없으므로 전체 포지션 종료로 대체
        from exchange.gate_sdk import close_position
        return close_position(symbol)
    else:
        from exchange.binance_api import cancel_order as binance_cancel_order
        return binance_cancel_order(symbol, order_id)