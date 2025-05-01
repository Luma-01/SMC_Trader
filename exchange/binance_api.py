import os
from binance.client import Client
from binance.enums import *
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)

def set_leverage(symbol: str, leverage: int):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
        client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
    except Exception as e:
        print(f"[ERROR] leverage 설정 실패: {symbol} - {e}")

def place_order(symbol: str, side: str, quantity: float):
    try:
        order = client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if side == 'buy' else SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=quantity
        )
        print(f"[ORDER] {symbol} {side.upper()} x{quantity}")
        return order
    except Exception as e:
        print(f"[ERROR] 주문 실패: {symbol} - {e}")
        return None

def get_open_position(symbol: str):
    try:
        positions = client.futures_position_information(symbol=symbol)
        pos_data = positions[0]
        amt = float(pos_data['positionAmt'])
        entry = float(pos_data['entryPrice'])

        if amt != 0:
            direction = 'long' if amt > 0 else 'short'
            return {
                'symbol': symbol,
                'direction': direction,
                'entry': entry
            }
    except Exception as e:
        print(f"[ERROR] 포지션 조회 실패: {symbol} - {e}")
    return None
