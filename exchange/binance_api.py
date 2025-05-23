# exchange/binance_api.py

import os
import math
from decimal import Decimal
from typing import Optional
from dotenv import load_dotenv
from notify.discord import send_discord_debug, send_discord_message
from binance.client import Client
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC
)
from binance.exceptions import BinanceAPIException
import time

load_dotenv()

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret, tld='com')
client.API_URL = "https://fapi.binance.com/fapi"
ORDER_TYPE_STOP_MARKET = 'STOP_MARKET'

# ────────────────────────────────────────────────
# ▸ 선물 **포지션 모드**(One-Way / Hedge) 캐싱
#   - Hedge 모드면 모든 주문에 `positionSide` 전달
# ────────────────────────────────────────────────
FUTURES_MODE_HEDGE: bool | None = None

def _ensure_mode_cached() -> None:
    """Binance 선물 계정의 포지션 모드를 1회만 조회-저장"""
    global FUTURES_MODE_HEDGE
    if FUTURES_MODE_HEDGE is None:
        info = client.futures_get_position_mode()
        FUTURES_MODE_HEDGE = bool(info["dualSidePosition"])

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

def place_order(symbol: str, side: str, quantity: float):
    """단순 시장 진입 (계정 모드에 맞춰 positionSide 자동 처리)"""
    try:
        _ensure_mode_cached()
        kwargs = dict(
            symbol=symbol,
            side=SIDE_BUY if side == "buy" else SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=quantity,
        )
        if FUTURES_MODE_HEDGE:
            kwargs["positionSide"] = "LONG" if side == "buy" else "SHORT"

        order = client.futures_create_order(**kwargs)
        msg = f"[ORDER] {symbol} {side.upper()} x{quantity} | 포지션: {side}"
        print(msg)
        send_discord_message(msg, "binance")
        return order
    
    except Exception as e:
        print(f"[ERROR] 주문 실패: {symbol} - {e}")
        send_discord_debug(f"[BINANCE] 주문 실패: {symbol} → {e}", "binance")
        return False
    
def place_order_with_tp_sl(
    symbol: str,
    side: str,
    quantity: float,
    tp: float,
    sl: float,
) -> bool:
    """
    ① 시장 주문이 바로 체결되지 않으면 5 초 동안 폴링  
    ② 증거금 부족(-2019) 시 수량을 10 %씩 줄여 최대 3회 재시도  
    ③ 실제 체결 수량으로 TP/SL 주문을 생성
    """
    try:
        _ensure_mode_cached()
        position_side = "LONG" if side == "buy" else "SHORT"
        base_kwargs = dict(
            symbol=symbol,
            side=SIDE_BUY if side == "buy" else SIDE_SELL,
            type=ORDER_TYPE_MARKET,
        )
        if FUTURES_MODE_HEDGE:
            base_kwargs["positionSide"] = position_side

        # ──────── 시장 진입 재시도 루프 ────────
        # ← LOT_SIZE 정보 미리 확보
        step   = float(get_tick_size(symbol) ** 0)  # tick → 0.0001 등, **0 = 1
        exch   = client.futures_exchange_info()
        prec   = 1
        for s in exch["symbols"]:
            if s["symbol"] == symbol.upper():
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])     # ex) 0.1
                        prec = abs(int(round(-1 * math.log10(step))))
                        break

        qty_try = round(quantity, prec)
        for attempt in range(3):
            try:
                entry_res = client.futures_create_order(
                    newOrderRespType="RESULT",   # 즉시 체결 정보 요청
                    quantity=qty_try,
                    **base_kwargs
                )
            except BinanceAPIException as e:
                # -2019 = 증거금 부족,  -4164 = notional 부족
                if e.code in (-2019, -4164) and attempt < 2:
                    factor   = 0.9 if e.code == -2019 else 1.1
                    qty_try  = math.floor(qty_try * factor / step) * step
                    qty_try  = round(qty_try, prec)
                    reason = "margin" if e.code == -2019 else "notional"
                    print(f"[RETRY] {reason} → 수량 {qty_try} 재시도({attempt+1}/3)")
                    continue
                raise

            # status == NEW → 5초 동안 체결 대기
            if entry_res["status"] == "NEW":
                order_id = entry_res["orderId"]
                t0 = time.time()
                while time.time() - t0 < 5:
                    o = client.futures_get_order(symbol=symbol, orderId=order_id)
                    if float(o["executedQty"]) > 0:
                        entry_res = o
                        break
                    time.sleep(0.2)
                else:   # 미체결 → 수량 축소 후 재시도
                    qty_try = math.floor(qty_try * 0.9 / step) * step
                    qty_try = round(qty_try, prec)
                    print(f"[RETRY] NEW→미체결 → 수량 {qty_try}")
                    continue
            break
        else:
            raise ValueError("시장 주문 반복 실패")

        filled_qty = float(entry_res["executedQty"])
        if filled_qty == 0:
            raise ValueError(f"시장 주문 미체결: {entry_res}")

        # ── ① 가격 자릿수 보정 ──────────────────────────
        tick = get_tick_size(symbol)                # e.g. Decimal('0.0001')
        tp = float(Decimal(str(tp)).quantize(tick)) # 4 decimals → 2.4544
        sl = float(Decimal(str(sl)).quantize(tick)) # 4 decimals

        # ── ② TP / SL 주문 생성 ─────────────────────────
        opposite_side = SIDE_SELL if side == "buy" else SIDE_BUY
        half_qty = math.floor((filled_qty / 2) / step) * step
        half_qty = round(half_qty, prec)
        tp_kwargs = dict(
            symbol      = symbol,
            side        = opposite_side,
            type        = ORDER_TYPE_LIMIT,
            timeInForce = TIME_IN_FORCE_GTC,
            quantity    = half_qty,
            price       = str(tp),
            reduceOnly  = True,
        )

        sl_qty = math.floor(filled_qty / step) * step
        sl_qty = round(sl_qty, prec)
        sl_kwargs = dict(
            symbol      = symbol,
            side        = opposite_side,
            type        = ORDER_TYPE_STOP_MARKET,
            stopPrice   = str(sl),
            quantity    = sl_qty,
            reduceOnly  = True,
        )
        if FUTURES_MODE_HEDGE:
            tp_kwargs["positionSide"] = position_side
            sl_kwargs["positionSide"] = position_side

        client.futures_create_order(**tp_kwargs)
        client.futures_create_order(**sl_kwargs)

        print(f"[TP/SL] {symbol} 진입 {filled_qty} → TP:{tp}, SL:{sl}")
        send_discord_message(
            f"[TP/SL] {symbol} 진입 {filled_qty} → TP:{tp}, SL:{sl}", "binance"
        )
        return True

    except Exception as e:
        print(f"[ERROR] TP/SL 포함 주문 실패: {symbol} - {e}")
        send_discord_debug(f"[BINANCE] TP/SL 포함 주문 실패: {symbol} → {e}", "binance")
        return False
    
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

def update_stop_loss_order(symbol: str, direction: str, stop_price: float):
    try:
        # ▸ SL 발행 전에도 계정 포지션 모드 확인
        _ensure_mode_cached()
        side = SIDE_SELL if direction == 'long' else SIDE_BUY
        position_side = 'LONG' if direction == 'long' else 'SHORT'
        kwargs = dict(
            symbol        = symbol,
            side          = side,
            type          = ORDER_TYPE_STOP_MARKET,
            stopPrice     = str(stop_price),
            closePosition = True,
            timeInForce   = TIME_IN_FORCE_GTC,
        )
        if FUTURES_MODE_HEDGE:
            kwargs["positionSide"] = position_side

        order = client.futures_create_order(**kwargs)
        msg = f"[SL 갱신] {symbol} STOP_MARKET SL 재설정 완료 → {stop_price}"
        print(msg)
        send_discord_debug(msg, "binance")
        return order['orderId']
    except Exception as e:
        msg = f"[ERROR] SL 갱신 실패: {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "binance")
        return False
    
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
        return False
        
# ✅ 사용 가능 잔고 조회 (USDT 기준)
def get_available_balance() -> float:
    try:
        balance = client.futures_account_balance()
        for asset in balance:
            if asset['asset'] == 'USDT':
                return float(asset['availableBalance'])
    except BinanceAPIException as e:
        print(f"[BINANCE] 잔고 조회 실패: {e}")
        send_discord_debug(f"[BINANCE] 잔고 조회 실패 → {e}", "binance")
    return 0.0


# 심볼별 수량 소수점 자리수 조회
def get_quantity_precision(symbol: str) -> int:
    try:
        exchange_info = client.futures_exchange_info()
        for s in exchange_info['symbols']:
            if s['symbol'] == symbol.upper():
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        precision = abs(int(round(-1 * math.log10(step_size))))
                        return precision
    except BinanceAPIException as e:
        print(f"[BINANCE] 수량 자리수 조회 실패: {e}")
        send_discord_debug(f"[BINANCE] 수량 자리수 조회 실패 → {e}", "binance")
    return 3  # 기본값

def get_tick_size(symbol: str) -> Decimal:
    try:
        exchange_info = client.futures_exchange_info()
        for s in exchange_info['symbols']:
            if s['symbol'] == symbol.upper():
                for f in s['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        return Decimal(f['tickSize'])
    except Exception as e:
        print(f"[BINANCE] tick_size 조회 실패: {e}")
        send_discord_debug(f"[BINANCE] tick_size 조회 실패 → {e}", "binance")
    return Decimal("0.0001")

def calculate_quantity(symbol: str, price: float, usdt_balance: float, leverage: int = 10) -> float:
    try:
        # 5 % 안전여유를 두어 증거금 부족 오류(code -2019)를 예방
        notional = usdt_balance * leverage * 0.95
        raw_qty = notional / price

        # stepSize / notional 최소값 가져오기
        exchange_info = client.futures_exchange_info()
        step_size = min_notional = None
        for s in exchange_info['symbols']:
            if s['symbol'] == symbol.upper():
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                    elif f['filterType'] == 'MIN_NOTIONAL':
                        min_notional = float(f['notional'])
        if step_size is None:
            print(f"[BINANCE] ❌ stepSize 조회 실패: {symbol}")
            return 0.0
        if min_notional is None:
            min_notional = 5.0     # 바이낸스 기본
        precision = abs(int(round(-1 * math.log10(step_size))))

        # ───── 명목가(min_notional) 만족하도록 보정 ─────
        steps = math.floor(raw_qty / step_size)
        notional = steps * step_size * price
        if notional < min_notional:
            needed_steps = math.ceil(min_notional / (step_size * price))
            steps = max(steps, needed_steps)
        qty = round(steps * step_size, precision)

        # 증거금 실제 가능 여부(5 % 여유)를 다시 체크
        if qty * price > usdt_balance * leverage * 0.95:
            return 0.0
        return qty
    except Exception as e:
        print(f"[BINANCE] ❌ 수량 계산 실패: {e}")
        return 0.0
