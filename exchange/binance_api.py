# exchange/binance_api.py

import os
from binance.client import Client
from dotenv import load_dotenv
from notify.discord import send_discord_debug, send_discord_message
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC
)



load_dotenv()

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret, tld='com')
client.API_URL = "https://fapi.binance.com/fapi"
ORDER_TYPE_STOP_MARKET = 'STOP_MARKET'

def set_leverage(symbol: str, leverage: int) -> None:
    try:
        client.futures_change_margin_type(symbol=symbol.upper(), marginType='ISOLATED')
    except Exception as e:
        if "No need to change margin type" not in str(e):
            msg = f"[ERROR] {symbol} 마진 타입 설정 실패 → {e}"
            print(msg)
            send_discord_debug(msg, "binance")
            
    try:
        client.futures_change_leverage(symbol=symbol.upper(), leverage=leverage)
    except Exception as e:
        print(f"[WARN] 레버리지 설정 실패: {symbol} → {e}")
        send_discord_debug(f"[BINANCE] 레버리지 설정 실패: {symbol} → {e}", "binance")

def get_max_leverage(symbol: str) -> int:
    try:
        brackets = client.futures_leverage_bracket()
        for entry in brackets:
            if entry["symbol"] == symbol.upper():
                lev = entry['brackets'][0]['initialLeverage']
                send_discord_debug(f"[LEVERAGE] {symbol} 최대 레버리지: {lev}", "binance")
                return int(lev)
            
    except Exception as e:
        print(f"[ERROR] 최대 레버리지 조회 실패 ({symbol}): {e}")
        send_discord_debug(f"[BINANCE] 최대 레버리지 조회 실패: {symbol} → {e}", "binance")
    return 20  # 기본값

def place_order(symbol: str, side: str, quantity: float, position_side: str = None):
    try:
        if position_side is None:
            position_side = position_side or ("LONG" if side == "buy" else "SHORT")
        order = client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if side == 'buy' else SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=quantity,
            positionSide=position_side
        )
        msg = f"[ORDER] {symbol} {side.upper()} x{quantity} | 포지션: {position_side}"
        print(msg)
        send_discord_message(msg, "binance")
        return order
    
    except Exception as e:
        print(f"[ERROR] 주문 실패: {symbol} - {e}")
        send_discord_debug(f"[BINANCE] 주문 실패: {symbol} → {e}", "binance")
        return None
    
def place_order_with_tp_sl(symbol: str, side: str, quantity: float, tp: float, sl: float):
    try:
        position_side = "LONG" if side == "buy" else "SHORT"
        opposite_side = SIDE_SELL if side == "buy" else SIDE_BUY
        orders = []

        # 진입
        orders.append(client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if side == 'buy' else SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=quantity,
            positionSide=position_side
        ))

        # TP
        orders.append(client.futures_create_order(
            symbol=symbol,
            side=opposite_side,
            type=ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=quantity / 2,
            price=str(tp),
            reduceOnly=True,
            positionSide=position_side
        ))

        # SL
        orders.append(client.futures_create_order(
            symbol=symbol,
            side=opposite_side,
            type=ORDER_TYPE_STOP_MARKET,
            stopPrice=str(sl),
            quantity=quantity,
            reduceOnly=True,
            positionSide=position_side
        ))
        print(f"[TP/SL] {symbol} 진입 및 TP/SL 설정 → TP: {tp}, SL: {sl}")
        send_discord_message(f"[TP/SL] {symbol} 진입 및 TP/SL 설정 → TP: {tp}, SL: {sl}", "binance")
        return orders
    
    except Exception as e:
        print(f"[ERROR] TP/SL 포함 주문 실패: {symbol} - {e}")
        send_discord_debug(f"[BINANCE] TP/SL 포함 주문 실패: {symbol} → {e}", "binance")
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

def place_stop_loss_order(symbol: str, direction: str, stop_price: float):
    try:
        side = SIDE_SELL if direction == 'long' else SIDE_BUY
        position_side = 'LONG' if direction == 'long' else 'SHORT'
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_STOP_MARKET,
            stopPrice=str(stop_price),
            closePosition=True,
            timeInForce=TIME_IN_FORCE_GTC,
            positionSide=position_side
        )
        msg = f"[SL] {symbol} {side} STOP_MARKET @ {stop_price}"
        print(msg)
        send_discord_debug(msg, "binance")
        return order['orderId']
    
    except Exception as e:
        print(f"[ERROR] SL 주문 실패: {symbol} - {e}")
        send_discord_debug(f"[BINANCE] SL 주문 실패: {symbol} → {e}", "binance")
        return None
    
def cancel_order(symbol: str, order_id: int):
    try:
        result = client.futures_cancel_order(symbol=symbol, orderId=order_id)
        msg = f"[CANCEL] {symbol} 주문 취소됨 (ID: {order_id})"
        print(msg)
        send_discord_debug(msg, "binance")
        return result
    
    except Exception as e:
        print(f"[ERROR] 주문 취소 실패: {symbol} - {e}")
        send_discord_debug(f"[BINANCE] 주문 취소 실패: {symbol} → {e}", "binance")
        return None
