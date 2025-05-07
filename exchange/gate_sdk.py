# exchange/gate_sdk.py

import os
from gate_api import ApiClient, Configuration, FuturesApi
from dotenv import load_dotenv
from notify.discord import send_discord_debug, send_discord_message
import math

load_dotenv()

config = Configuration(
    key=os.getenv("GATE_API_KEY"),
    secret=os.getenv("GATE_API_SECRET"),
    host="https://api.gateio.ws/api/v4"
)

client = ApiClient(config)
futures_api = FuturesApi(client)

def place_order(symbol: str, side: str, size: float, leverage: int = 20):
    try:
        order = {
            "contract": symbol,
            "size": size if side == "buy" else -size,
            "price": 0,
            "tif": "ioc",
            "reduce_only": False,
            "auto_size": "",
            "text": "SMC-BOT",
            "leverage": leverage
        }
        response = futures_api.create_futures_order(order)
        msg = f"[ORDER] {symbol} {side.upper()} x{size} | 레버리지: {leverage}"
        print(msg)
        send_discord_message(msg, "gateio")
        send_discord_debug(f"[GATE] 주문 전송됨: {symbol} {side.upper()} x{size}", "gateio")
        return response
    
    except Exception as e:
        msg = f"[ERROR] 주문 실패: {symbol} {side.upper()} x{size} → {e}"
        print(msg)
        send_discord_debug(msg, "gateio")
        return None

def get_open_position(symbol: str):
    try:
        positions = futures_api.list_futures_positions()
        for p in positions:
            if p.contract == symbol and float(p.size) != 0:
                direction = 'long' if float(p.size) > 0 else 'short'
                return {
                    "symbol": symbol,
                    "direction": direction,
                    "entry": float(p.entry_price)
                }
    except Exception as e:
        msg = f"[ERROR] 포지션 조회 실패: {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "gateio")

    return None
    
# 사용 가능 잔고 조회 (USDT 기준)
def get_available_balance() -> float:
    try:
        accounts = futures_api.list_futures_accounts()
        for acc in accounts:
            if acc.currency == 'USDT':
                return float(acc.available)
    except Exception as e:
        print(f"[GATE] 잔고 조회 실패: {e}")
        send_discord_debug(f"[GATE] 잔고 조회 실패 → {e}", "gateio")
    return 0.0

# 수량 소수점 자리수 계산
def get_quantity_precision(symbol: str) -> int:
    try:
        contract = futures_api.get_futures_contract(symbol)
        step = float(contract.order_size_min)
        precision = abs(int(round(-1 * math.log10(step))))
        return precision
    except Exception as e:
        print(f"[GATE] 수량 precision 조회 실패: {e}")
        send_discord_debug(f"[GATE] 수량 precision 조회 실패 → {e}", "gateio")
    return 3