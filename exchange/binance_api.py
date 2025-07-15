# exchange/binance_api.py

import time
import os
import math
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from config.settings import TRADE_RISK_PCT
from typing import Optional
from dotenv import load_dotenv
from notify.discord import send_discord_debug, send_discord_message
from binance.client import Client
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC
)
from binance.exceptions import BinanceAPIException

load_dotenv()

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret, tld='com')
client.API_URL = "https://fapi.binance.com/fapi"
ORDER_TYPE_STOP_MARKET = 'STOP_MARKET'
ORDER_TYPE_LIMIT       = 'LIMIT'   # ← 이미 import 됐지만 가독성용

# ════════════════════════════════════════════════════════
# get_mark_price: SL 내부 로직용으로 markPrice 가져오기
# ════════════════════════════════════════════════════════
def _to_binance_symbol(sym: str) -> str:
    """
    Gate → Binance 선물 심볼 변환
      'ETH_USDT'  -> 'ETHUSDT'
      'ETH/USDT'  -> 'ETHUSDT'
    이미 Binance 형식이면 그대로 반환
    """
    sym = sym.upper()
    if '_' in sym:
        sym = sym.replace('_USDT', 'USDT').replace('_', '')
    return sym

def get_mark_price(symbol: str) -> float:
    """현재 마크 가격(markPrice) 반환. 실패 시 마지막 체결가로 폴백."""
    try:
        b_sym = _to_binance_symbol(symbol)
        resp = client.futures_mark_price(symbol=b_sym)
        return float(resp.get("markPrice", resp.get("price", 0)))
    except Exception as e:
        print(f"[ERROR] mark price fetch failed: {symbol} → {e}")
        send_discord_debug(f"[BINANCE] mark price fetch failed: {symbol} → {e}", "binance")
        # 폴백: ticker 마지막 가격
        try:
            tk = client.futures_symbol_ticker(symbol=b_sym)
            return float(tk.get("price", 0))
        except:
            return 0.0

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

        # ── ① 가격 자릿수 보정 + Δ≥1 tick 확보 ────────────
        tick = get_tick_size(symbol)                        # Decimal

        # 기본 라운딩
        if side == "buy":                                   # LONG
            tp_dec = Decimal(str(tp)).quantize(tick, ROUND_UP)
            sl_dec = Decimal(str(sl)).quantize(tick, ROUND_DOWN)
        else:                                               # SHORT
            tp_dec = Decimal(str(tp)).quantize(tick, ROUND_DOWN)
            sl_dec = Decimal(str(sl)).quantize(tick, ROUND_UP)

        # 체결 평균가(혹은 첫 fill 가격) 확보
        last_price = Decimal(str(
            entry_res.get("avgPrice") or entry_res["fills"][0]["price"]
        ))

        # *** 최소 1 tick 차이 보정 ***
        if side == "buy" and tp_dec - last_price < tick:    # LONG TP ↑
            tp_dec = last_price + tick
        if side == "sell" and last_price - tp_dec < tick:   # SHORT TP ↓
            tp_dec = last_price - tick
 
        # SL은 STOP_MARKET이므로 배수만 맞으면 충분 → Δ 확인 불필요   # ↑

        tp_str = format(tp_dec, 'f')
        sl_str = format(sl_dec, 'f')

        # DEBUG
        print(f"[DEBUG] {symbol} tick={tick}, tp={tp_str}, sl={sl_str}")

        # ── ② TP / SL 주문 생성 ─────────────────────────
        opposite_side = SIDE_SELL if side == "buy" else SIDE_BUY
        # ── TP 수량 산정 ────────────────────────────────
        half_qty_raw = filled_qty / 2
        # ▸ 항상 **버림**(floor) → 50 % 이하만 TP 로 잡히도록
        half_qty     = math.floor(half_qty_raw / step) * step
        half_qty     = round(half_qty, prec)

        # ▸ stepSize 미만이면 최소 1-step, 하지만 **전량은 아님**
        if half_qty < step:
            half_qty = step if filled_qty > step else filled_qty

        # ── 바이낸스 MIN_NOTIONAL 필터 재검증 ────────────
        min_notional_tp = None
        for s in exch["symbols"]:
            if s["symbol"] == symbol.upper():
                for f in s["filters"]:
                    if f["filterType"] == "MIN_NOTIONAL":
                        min_notional_tp = float(f["notional"])
                        break
                break

        # ─── MIN_NOTIONAL 보정 로직 개편 ─────────────────────
        # ① half_qty 로는 5 USDT 를 못 넘길 때,
        # ② ‘필요 최소 수량’만큼만 늘리되 **전량을 초과하지 않음**.
        if min_notional_tp and half_qty * float(tp) < min_notional_tp:
            # 5 USDT / 가격 → 필요 계약수 → stepSize 로 올림
            need_steps = math.ceil(min_notional_tp / (float(tp) * step))
            adj_qty    = need_steps * step
            adj_qty    = round(adj_qty, prec)
            # 그래도 절반보다 작으면 절반 사용, 절반보다 크지만 전량보다 크면 전량 한도
            half_qty   = max(adj_qty, half_qty)
            half_qty   = min(half_qty, filled_qty)
            # step 크기보다 작게 남는다면(=시장가치가 5 USDT 미만) 그냥 전량
            if half_qty < step:
                half_qty = round(math.floor(filled_qty / step) * step, prec)
            
        tp_kwargs = dict(
            symbol      = symbol,
            side        = opposite_side,
            type        = ORDER_TYPE_LIMIT,
            timeInForce = TIME_IN_FORCE_GTC,
            quantity    = half_qty,
            price       = tp_str,
            reduceOnly  = True,
        )

        sl_qty = math.floor(filled_qty / step) * step
        sl_qty = round(sl_qty, prec)
        sl_kwargs = dict(
            symbol      = symbol,
            side        = opposite_side,
            type        = ORDER_TYPE_STOP_MARKET,
            stopPrice   = sl_str,
            quantity    = sl_qty,
            reduceOnly  = True,
        )
        if FUTURES_MODE_HEDGE:
            tp_kwargs["positionSide"] = position_side
            sl_kwargs["positionSide"] = position_side

        # TP 지정가 주문
        client.futures_create_order(**tp_kwargs)
        # SL 주문은 update_stop_loss_order() 에서 일괄 관리하므로
        # 이 지점에서는 SL 생성 로직을 비활성화합니다.
        # client.futures_create_order(**sl_kwargs)

        print(f"[TP/SL] {symbol} 진입 {filled_qty} → TP:{tp_str}, SL:{sl_str}")
        send_discord_message(
            f"[TP/SL] {symbol} 진입 {filled_qty} → TP:{tp_str}, SL:{sl_str}", "binance"
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
        # ▸ SL 가격도 tick 에 맞춰 재정규화
        tick = get_tick_size(symbol)
        if direction == 'long':
            stop_dec = Decimal(str(stop_price)).quantize(tick, rounding=ROUND_DOWN)
        else:
            stop_dec = Decimal(str(stop_price)).quantize(tick, rounding=ROUND_UP)
        # ▸ 마크가격 조회 → futures_mark_price 로 교체
        mark_price = float(
            client.futures_mark_price(symbol=symbol)["markPrice"]
        )
        tick_f = float(tick)

        # ── 최소 버퍼: markPrice 와 ≥ BUFFER_TICKS × tickSize 이상 간격 확보 ──
        BUFFER_TICKS = 3                         # ← 필요하면 2~5 사이 조정
        if direction == "long":
            limit_price = Decimal(str(mark_price - tick_f * BUFFER_TICKS))
            if stop_dec >= limit_price:
                stop_dec = limit_price.quantize(tick, ROUND_DOWN)
        else:  # short
            limit_price = Decimal(str(mark_price + tick_f * BUFFER_TICKS))
            if stop_dec <= limit_price:
                stop_dec = limit_price.quantize(tick, ROUND_UP)

        stop_str = format(stop_dec, "f")

        kwargs = dict(
            symbol      = symbol,
            side        = side,
            type        = ORDER_TYPE_STOP_MARKET,
            stopPrice   = stop_str,
            workingType = "MARK_PRICE",      # ← 즉시 트리거 방지
            closePosition = True,
            timeInForce   = TIME_IN_FORCE_GTC,
        )
        if FUTURES_MODE_HEDGE:
            kwargs["positionSide"] = position_side

        # ── ① 새 SL 주문 생성  (실패시 예외 발생) ────────────────────────
        order = client.futures_create_order(**kwargs)

        new_id = order["orderId"]

        # ── ② “다른” STOP-MARKET 주문은 모두 취소  ──────────────────────
        try:
            for o in client.futures_get_open_orders(symbol=symbol):
                if (
                    o["type"] == ORDER_TYPE_STOP_MARKET and
                    (o.get("reduceOnly") or o.get("closePosition")) and
                    o["orderId"] != new_id
                ):
                    try:
                        client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                        print(f"[CANCEL] {symbol} SL 주문 취소됨 (ID: {o['orderId']})")
                    except BinanceAPIException as ce:
                        if ce.code != -2011:        # –2011 = Unknown order → 무시
                            raise
        except Exception as e:
            print(f"[WARN] SL 취소 실패: {e}")
            send_discord_debug(f"[BINANCE] SL 취소 실패 → {e}", "binance")
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
        
# ════════════════════════════════════════════════════════
#  잔고 관련 유틸
# ════════════════════════════════════════════════════════
# ✅ ① ‘사용 가능’(free) 잔고 – 기존 함수 유지

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


# ✅ ② ‘전체’(free + 포지션증거금) 잔고 – 새로 추가
def get_total_balance() -> float:
    """
    포지션 증거금을 포함한 **지갑 총 잔고**(USDT) 반환  
    futures_account_balance() 리턴 값 중  
    └ availableBalance = free,   balance = free + margin
    """
    try:
        balance = client.futures_account_balance()
        for asset in balance:
            if asset["asset"] == "USDT":
                return float(asset["balance"])          # ← 전체
    except BinanceAPIException as e:
        print(f"[BINANCE] 총 잔고 조회 실패: {e}")
        send_discord_debug(f"[BINANCE] 총 잔고 조회 실패 → {e}", "binance")
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
                        # ex) "0.01000000" → Decimal('0.01')
                        # 후행 0 제거(normalize)로 정확한 tick 단위를 확보
                        return Decimal(f['tickSize']).normalize()
    except Exception as e:
        print(f"[BINANCE] tick_size 조회 실패: {e}")
        send_discord_debug(f"[BINANCE] tick_size 조회 실패 → {e}", "binance")
    return Decimal("0.0001")

def calculate_quantity(
    symbol: str,
    price: float,
    usdt_balance: float,
    leverage: int = 10,
) -> float:
    try:
        # ────────────────  진입 비중 설정  ────────────────
        # settings.TRADE_RISK_PCT 를 단일-소스로 사용
        margin_to_use = usdt_balance * TRADE_RISK_PCT  # 사용할 증거금
        notional = margin_to_use * leverage            # 실제 포지션 크기
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
        
        # ────── 절반 익절 고려 최소 포지션 사이즈 보장 ──────
        # 절반 익절 후에도 의미 있는 물량이 남도록 최소 4 step 보장
        min_steps_for_half_exit = 4
        if steps < min_steps_for_half_exit:
            # 증거금 여유가 있다면 최소 사이즈로 조정
            needed_notional = min_steps_for_half_exit * step_size * price
            if needed_notional <= usdt_balance * leverage * 0.9:  # 10% 여유 두고 확인
                steps = min_steps_for_half_exit
                print(f"[BINANCE] 절반 익절 고려 최소 사이즈 적용: {steps} steps")
            else:
                print(f"[BINANCE] ⚠️ 절반 익절 고려 시 증거금 부족: steps={steps}")
        
        qty = round(steps * step_size, precision)

        # 증거금 실제 가능 여부(5 % 여유)를 다시 체크
        if qty * price > usdt_balance * leverage * 0.95:
            return 0.0
        return qty
    except Exception as e:
        print(f"[BINANCE] ❌ 수량 계산 실패: {e}")
        return 0.0

# ─────────────────────────────────────────────────────────────
#  NEW : TP(리미트) 주문 갱신/재발주   ★
# ─────────────────────────────────────────────────────────────
def update_take_profit_order(symbol: str, direction: str, take_price: float):
    """
    ▸ 기존 reduce-only LIMIT(TP) 주문을 모두 취소한 뒤  
      절반 포지션만큼 새 TP 주문을 넣는다.  
    ▸ 가격은 tickSize 에 맞춰 라운딩.
    반환값 : 새 주문의 orderId (실패 시 False)
    """
    try:
        _ensure_mode_cached()

        # ① 가격 라운딩
        tick = get_tick_size(symbol)
        if direction == "long":
            tp_dec = Decimal(str(take_price)).quantize(tick, ROUND_UP)
            side   = SIDE_SELL
            pos_side = "LONG"
        else:
            tp_dec = Decimal(str(take_price)).quantize(tick, ROUND_DOWN)
            side   = SIDE_BUY
            pos_side = "SHORT"
        tp_str = format(tp_dec, "f")

        # ② 포지션 수량 확인
        pos_info = client.futures_position_information(symbol=symbol)[0]
        qty_full = abs(float(pos_info["positionAmt"]))
        if qty_full == 0:
            return False

        # 기본 정책 : 절반 익절
        step  = float(get_tick_size(symbol) ** 0)  # = 1.0 (수량 반올림용)
        prec  = get_quantity_precision(symbol)
        
        # ────── 절반 익절 로직 (단순화) ──────────────────────────────
        # 정확한 절반 익절만 수행
        qty_half = qty_full / 2
        qty_tp_raw = round(qty_half, prec)
        
        if qty_tp_raw >= step:
            qty = qty_tp_raw
            remaining_qty = qty_full - qty
            print(f"[BINANCE] 절반 익절: {qty}/{qty_full} (남은 물량: {remaining_qty})")
        else:
            # 절반 익절이 stepSize보다 작으면 전량 TP
            qty = qty_full
            print(f"[BINANCE] ⚠️ 전량 익절 (절반이 stepSize 미달): {qty}/{qty_full}")
        
        # 최종 검증: stepSize 미달이면 전량 TP
        if qty < step:
            qty = qty_full
            print(f"[BINANCE] ⚠️ stepSize 미달로 전량 익절: {qty}/{qty_full}")
        
        # 정밀도 맞추기
        qty = round(qty, prec)

        # ③ 기존 reduce-only LIMIT 주문 취소
        try:
            for od in client.futures_get_open_orders(symbol=symbol):
                if od["type"] == ORDER_TYPE_LIMIT and od.get("reduceOnly"):
                    client.futures_cancel_order(symbol=symbol,
                                                orderId=od["orderId"])
        except Exception:
            pass

        # ④ 새 TP 주문 발행
        kwargs = dict(
            symbol      = symbol,
            side        = side,
            type        = ORDER_TYPE_LIMIT,
            price       = tp_str,
            timeInForce = TIME_IN_FORCE_GTC,
            quantity    = qty,
            reduceOnly  = True,
        )
        if FUTURES_MODE_HEDGE:
            kwargs["positionSide"] = pos_side

        res = client.futures_create_order(**kwargs)
        print(f"[TP 갱신] {symbol} LIMIT TP 재설정 완료 → {tp_str}")
        send_discord_debug(f"[TP 갱신] {symbol} LIMIT TP 재설정 완료 → {tp_str}", "binance")
        return res["orderId"]

    except Exception as e:
        print(f"[ERROR] TP 갱신 실패: {symbol} → {e}")
        send_discord_debug(f"[ERROR] TP 갱신 실패: {symbol} → {e}", "binance")
        return False
