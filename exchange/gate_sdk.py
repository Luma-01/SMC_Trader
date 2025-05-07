# exchange/gate_sdk.py

import os
from decimal import Decimal
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
    
# TP/SL 포함 주문
def place_order_with_tp_sl(symbol: str, side: str, size: float, tp: float, sl: float, leverage: int = 20):
    try:
        # 진입
        entry_order = {
            "contract": symbol,
            "size": size if side == "buy" else -size,
            "price": 0,
            "tif": "ioc",
            "reduce_only": False,
            "auto_size": "",
            "text": "SMC-BOT",
            "leverage": leverage
        }
        futures_api.create_futures_order(entry_order)

        # TP
        tp_order = {
            "contract": symbol,
            "size": round(size / 2, 3) if side == "buy" else round(-size / 2, 3),
            "price": tp,
            "tif": "gtc",
            "reduce_only": True,
            "text": "TP-SMC"
        }
        futures_api.create_futures_order(tp_order)

        # SL (마크가격 기준 스탑 마켓)
        sl_order = {
            "contract": symbol,
            "size": size if side == "buy" else -size,
            "price": 0,
            "tif": "gtc",
            "reduce_only": True,
            "text": "SL-SMC",
            "stop": {
                "price": sl,
                "type": "mark_price"
            }
        }
        futures_api.create_futures_order(sl_order)

        msg = f"[TP/SL] {symbol} 진입 및 TP/SL 설정 완료 → TP: {tp}, SL: {sl}"
        print(msg)
        send_discord_message(msg, "gateio")
        return True

    except Exception as e:
        msg = f"[ERROR] TP/SL 포함 주문 실패: {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "gateio")
        return False
        
def update_stop_loss_order(symbol: str, direction: str, stop_price: float):
    try:
        pos = futures_api.get_futures_position(symbol)
        size = float(pos.size)
        if size == 0:
            return None

        sl_order = {
            "contract": symbol,
            "size": size if direction == 'long' else -size,
            "tif": "gtc",
            "reduce_only": True,
            "text": "SL-UPDATE",
            "trigger": {
                "price": stop_price,
                "rule": 2  # mark price 기준 트리거
            }
        }
        futures_api.create_futures_order(sl_order)
        msg = f"[SL 갱신] {symbol} SL 재설정 완료 → {stop_price}"
        print(msg)
        send_discord_debug(msg, "gateio")
        return True
    except Exception as e:
        msg = f"[ERROR] SL 갱신 실패: {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "gateio")
        return None
    
def close_position(symbol: str):
    try:
        pos = futures_api.get_futures_position(symbol)
        size = float(pos.size)
        if size == 0:
            return False

        close_order = {
            "contract": symbol,
            "size": -size,
            "tif": "ioc",
            "reduce_only": True,
            "text": "FORCE-CLOSE"
        }
        futures_api.create_futures_order(close_order)
        print(f"[GATE] 포지션 강제 종료 완료 | {symbol}")
        send_discord_debug(f"[GATE] 포지션 강제 종료 완료 | {symbol}", "gateio")
        return True
    except Exception as e:
        msg = f"[GATE] ❌ 포지션 종료 실패 | {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "gateio")
        return False

def get_tick_size(symbol: str) -> Decimal:
    try:
        contract = futures_api.get_futures_contract(symbol)
        return Decimal(str(contract.order_price_round))
    except Exception as e:
        print(f"[GATE] tick_size 조회 실패: {e}")
        send_discord_debug(f"[GATE] tick_size 조회 실패 → {e}", "gateio")
    return Decimal("0.0001")
