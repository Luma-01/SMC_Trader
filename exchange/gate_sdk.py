# exchange/gate_sdk.py

import os
from time import time, sleep
from decimal import Decimal
from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
from dotenv import load_dotenv
from notify.discord import send_discord_debug, send_discord_message
import math

load_dotenv()

Configuration.set_default_configuration(Configuration(
    key=os.getenv("GATEIO_API_KEY"),
    secret=os.getenv("GATEIO_API_SECRET"),
    host="https://api.gateio.ws/api/v4"
))

api_client = ApiClient()
futures_api = FuturesApi(api_client)

def set_leverage(symbol: str, leverage: int):
    try:
        contract_symbol = normalize_contract_symbol(symbol)
        futures_api.update_position_leverage(settle='usdt', contract=contract_symbol, leverage=leverage)
        print(f"[GATE] 레버리지 설정 완료: {symbol} → x{leverage}")
        send_discord_debug(f"[GATE] 레버리지 설정 완료: {symbol} → x{leverage}", "gateio")
        return True
    except Exception as e:
        msg = f"[GATE] 레버리지 설정 실패: {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "gateio")
        return False

def place_order(symbol: str, side: str, size: float, leverage: int = 20):
    try:
        set_leverage(symbol, leverage)
        order = FuturesOrder(
            contract=normalize_contract_symbol(symbol),
            size=size if side == "buy" else -size,
            price=0,
            tif="ioc",
            reduce_only=False,
            auto_size="",
            text="t-SMC-BOT"
        )
        response = futures_api.create_futures_order(settle='usdt', futures_order=order)
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

def get_open_position(symbol: str, retry: int = 10, delay: float = 1.5):
    contract_symbol = normalize_contract_symbol(symbol)
    for attempt in range(retry):
        try:
            position = futures_api.get_position(settle='usdt', contract=contract_symbol)
            size = float(position.size or 0)
            if abs(size) > 0:
                entry = float(position.entry_price) if position.entry_price else 0.0
                direction = 'long' if size > 0 else 'short'
                return {
                    "symbol": symbol,
                    "direction": direction,
                    "entry": entry,
                    "size": size
                }
        except Exception as e:
            print(f"[RETRY {attempt + 1}/{retry}] 포지션 조회 실패: {symbol} → {e}")
            sleep(delay)
    msg = f"[ERROR] 포지션 조회 실패: {symbol} → 최종 재시도 실패"
    print(msg)
    send_discord_debug(msg, "gateio")
    return None

    
# 사용 가능 잔고 조회 (USDT 기준)
def get_available_balance() -> float:
    try:
        account = futures_api.list_futures_accounts(settle='usdt')
        return float(account.available)
    except Exception as e:
        print(f"[GATE] 잔고 조회 실패: {e}")
        send_discord_debug(f"[GATE] 잔고 조회 실패 → {e}", "gateio")
    return 0.0

# 수량 소수점 자리수 계산
def get_quantity_precision(symbol: str) -> int:
    try:
        return get_contract_precision(symbol)
    except Exception as e:
        print(f"[GATE] 수량 precision 조회 실패: {e}")
        send_discord_debug(f"[GATE] 수량 precision 조회 실패 → {e}", "gateio")
    return 3

def get_contract_precision(symbol: str) -> int:
    contract_symbol = normalize_contract_symbol(symbol)
    contract = futures_api.get_futures_contract(settle='usdt', contract=contract_symbol)
    step = float(contract.order_size_min)
    return abs(int(round(-1 * math.log10(step))))

def normalize_contract_symbol(symbol: str) -> str:
    return symbol.replace("USDT", "_USDT")

# TP/SL 포함 주문
def place_order_with_tp_sl(symbol: str, side: str, size: float, tp: float, sl: float, leverage: int = 20):
    tick = get_tick_size(symbol)
    tp = float(Decimal(str(tp)).quantize(tick))
    sl = float(Decimal(str(sl)).quantize(tick))
    set_leverage(symbol, leverage)
    contract_symbol = normalize_contract_symbol(symbol)
    try:
        contract = futures_api.get_futures_contract(settle='usdt', contract=contract_symbol)
    except Exception as e:
        print(f"[ERROR] 계약 정보 조회 실패: {e}")
        send_discord_debug(f"[ERROR] 계약 정보 조회 실패: {e}", "gateio")
        return False

    # 디버깅: 환경 변수 체크
    print("[DEBUG] API KEY:", os.getenv("GATEIO_API_KEY"))
    print("[DEBUG] API SECRET:", os.getenv("GATEIO_API_SECRET"))

    try:
        # 진입 주문
        entry_order = FuturesOrder(
            contract=contract_symbol,
            size=size if side == "buy" else -size,
            price="0",
            tif="ioc",
            reduce_only=False,
            text="t-SMC-BOT"
        )
        entry_res = futures_api.create_futures_order(settle='usdt', futures_order=entry_order)
        print(f"[DEBUG] entry_res = {entry_res}")
        if not entry_res or float(entry_res.size or 0) == 0:
            raise Exception("진입 주문 미체결 (응답에서 size 없음)")
        
        # 포지션 체결 대기 (최대 15초 대기)
        pos = None
        timeout = time() + 15
        while time() < timeout:
            pos = get_open_position(symbol)
            if pos:
                break
            print(f"[WAIT] 포지션 반영 대기 중... {symbol}")
            sleep(1)
            
        if not pos:
            print(f"[WARNING] 포지션 확인 실패, 주문 응답 기반 TP/SL 생성 시도")
            confirmed_size = abs(float(entry_res.size or size))
            entry_price = float(entry_res.fill_price or 0.0)
            direction = "long" if confirmed_size > 0 else "short"
        else:
            confirmed_size = abs(float(pos["size"]))
            entry_price = float(pos["entry"])
            direction = pos["direction"]

        confirmed_size = abs(size)  # 기본값
        try:
            gate_pos = futures_api.get_position(settle="usdt", contract=contract)
            confirmed_size = abs(float(gate_pos.size))
        except Exception as e:
            print(f"[GATE] 포지션 확인 실패: {e}")

        step_size = float(contract.order_size_min)
        precision = get_contract_precision(symbol)
        tp_size = round(math.floor((confirmed_size / 2) / step_size) * step_size, precision)
        sl_size = round(math.floor(confirmed_size / step_size) * step_size, precision)

        # TP 주문
        tp_order = FuturesOrder(
            contract=contract_symbol,
            size=tp_size if side == "buy" else -tp_size,
            price=str(tp),
            tif="gtc",
            reduce_only=True,
            text="t-TP-SMC"
        )
        if tp_size <= 0:
            raise Exception("TP 주문 수량이 0 이하")
        tp_res = futures_api.create_futures_order(settle='usdt', futures_order=tp_order)
        if not tp_res:
            raise Exception("TP 주문 실패")

        # SL 주문
        sl_order = FuturesOrder(
            contract=contract_symbol,
            size=sl_size if side == "buy" else -sl_size,
            price="0",
            tif="gtc",
            reduce_only=True,
            text="t-SL-SMC",
            stop={"price": str(sl), "type": "mark_price"}
        )
        if sl_size <= 0:
            raise Exception("SL 주문 수량이 0 이하")
        sl_res = futures_api.create_futures_order(settle='usdt', futures_order=sl_order)
        if not sl_res:
            raise Exception("SL 주문 실패")

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
        contract = normalize_contract_symbol(symbol)
        pos = futures_api.get_position(settle='usdt', contract=contract)
        size = float(pos.size)
        if size == 0:
            return None

        tick = get_tick_size(symbol)
        normalized_stop = float(Decimal(str(stop_price)).quantize(tick))
        sl_order = FuturesOrder(
            contract=normalize_contract_symbol(symbol),
            size=size if direction == 'long' else -size,
            tif="gtc",
            reduce_only=True,
            text="t-SL-UPDATE",
            trigger={"price": normalized_stop, "rule": 2}
        )

        futures_api.create_futures_order(settle='usdt', futures_order=sl_order)

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
        contract = normalize_contract_symbol(symbol)
        pos = futures_api.get_position(settle='usdt', contract=contract)
        size = float(pos.size)
        if size == 0:
            return False

        close_order = FuturesOrder(
            contract=normalize_contract_symbol(symbol),
            size=-size,
            tif="ioc",
            reduce_only=True,
            text="t-FORCE-CLOSE"
        )
        futures_api.create_futures_order(settle='usdt', futures_order=close_order)
        
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
        contract_symbol = normalize_contract_symbol(symbol)
        contract = futures_api.get_futures_contract(settle='usdt', contract=contract_symbol)
        return Decimal(str(contract.order_price_round))
    except Exception as e:
        print(f"[GATE] tick_size 조회 실패: {e}")
        send_discord_debug(f"[GATE] tick_size 조회 실패 → {e}", "gateio")
    return Decimal("0.0001")

def calculate_quantity_gate(symbol: str, price: float, usdt_balance: float, leverage: int = 10) -> float:
    try:
        from math import floor
        notional = usdt_balance * leverage
        raw_qty = notional / price
        contract_symbol = normalize_contract_symbol(symbol)  # ✅ Gate 심볼 포맷 변환
        contract = futures_api.get_futures_contract(settle='usdt', contract=contract_symbol)
        step_size = float(contract.order_size_min)
        precision = get_contract_precision(symbol)
        steps = floor(raw_qty / step_size)
        qty = round(steps * step_size, precision)

        if qty < step_size:
            print(f"[GATE] 최소 주문 수량 미달: 계산된 qty={qty}, 최소={step_size}")
            return 0.0

        print(f"[GATE] 수량 계산 → raw_qty={raw_qty}, steps={steps}, qty={qty}")

        return qty
    except Exception as e:
        print(f"[GATE] ❌ 수량 계산 실패: {e}")
        send_discord_debug(f"[GATE] ❌ 수량 계산 실패: {e}", "gateio")
        return 0.0

