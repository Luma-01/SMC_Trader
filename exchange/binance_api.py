import os
from binance.client import Client
from binance.enums import *
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret, tld='com')
client.API_URL = "https://fapi.binance.com/fapi"

print("[BINANCE API] Loaded key:", os.getenv("BINANCE_API_KEY"))

def set_leverage(symbol: str, leverage: int) -> None:
    try:
        # margin type 강제 설정
        client.futures_change_margin_type(symbol=symbol.upper(), marginType='ISOLATED')
    except Exception as e:
        if "No need to change margin type" not in str(e):
            print(f"[WARN] 마진 타입 변경 실패: {symbol} → {e}")

    try:
        client.futures_change_leverage(symbol=symbol.upper(), leverage=leverage)
    except Exception as e:
        print(f"[WARN] 레버리지 설정 실패: {symbol} → {e}")

def get_max_leverage(symbol: str) -> int:
    try:
        data = client._request("get", "/fapi/v1/leverageBracket", signed=True)
        if isinstance(data, list):  # ← 명확하게 타입 확인
            for entry in data:
                if entry["symbol"] == symbol.upper():
                    return int(entry["brackets"][0]["initialLeverage"])
        else:
            print(f"[ERROR] unexpected response type: {type(data)} -> {data}")
    except Exception as e:
        print(f"[ERROR] 최대 레버리지 조회 실패 ({symbol}): {e}")
    return 20  # 기본값

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
        if not positions:
            return None
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
        raise e
    return None
