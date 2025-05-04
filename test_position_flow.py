# test_position_flow.py

from decimal import Decimal, ROUND_DOWN
from binance.client import Client
from core.position import PositionManager
from exchange.binance_api import set_leverage, place_order
import pandas as pd
import time

pm = PositionManager()
symbol = "XRPUSDT"
qty = 10
leverage = 20
client = Client()

def get_tick_size(symbol: str) -> Decimal:
    info = client.futures_exchange_info()
    for s in info['symbols']:
        if s['symbol'] == symbol:
            for f in s['filters']:
                if f['filterType'] == 'PRICE_FILTER':
                    return Decimal(f['tickSize'])
    raise Exception(f"Tick size not found for symbol: {symbol}")

print("=== 레버리지 설정 ===")
set_leverage(symbol, leverage)

print("=== 포지션 진입 ===")
price = Decimal(client.futures_symbol_ticker(symbol=symbol)['price'])
tick_size = get_tick_size(symbol)
entry = price
sl = (entry * Decimal("0.99")).quantize(tick_size, rounding=ROUND_DOWN)
tp = (entry * Decimal("1.015")).quantize(tick_size, rounding=ROUND_DOWN)
pm.enter(symbol, direction="long", entry=entry, sl=sl, tp=tp)

# 실 주문
print("=== 실 거래 진입 === (롱 진입)")
place_order(symbol, side="buy", quantity=qty, position_side="LONG")

print("=== MSS 보호선 유도 ===")
data = {
    "time": pd.date_range(end="2025-05-03", periods=10, freq="1min"),
    "open": [99, 100, 101, 102, 103, 104, 105, 106, 107, 108],
    "high": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
    "low":  [98,  99, 100, 101, 102, 103, 104, 105, 106, 107],
    "close":[99, 100, 101, 102, 103, 104, 105, 106, 107, 108],
}
ltf_df = pd.DataFrame(data)
ltf_df.attrs["symbol"] = symbol
pm.update_price(symbol, current_price=108, ltf_df=ltf_df)
time.sleep(1)

print("=== TP 도달 테스트 ===")
pm.update_price(symbol, current_price=111, ltf_df=ltf_df)
time.sleep(1)

print("=== 보호선 이탈 테스트 ===")
pm.update_price(symbol, current_price=94, ltf_df=ltf_df)
time.sleep(1)

print("=== SL 테스트 === (롱 손절)")
pm.update_price(symbol, current_price=94.0)  # 기존 롱 SL = 95 → SL 트리거
