# exchange/gate_sdk.py

import json
import os
from time import time, sleep
from decimal import Decimal
# ------------------------------------------------------------------
# ❶ 환경/로깅 세팅
#    - 패키지 트리 밖에서 단독 실행할 때 `notify.discord` 가 없으면
#      더미 함수로 대체해 테스트가 끊기지 않도록 처리
# ------------------------------------------------------------------
from dotenv import load_dotenv
try:                                  # 정상 앱 실행 경로
    from notify.discord import send_discord_debug, send_discord_message
except ModuleNotFoundError:           # 단독 실행(디버깅) 시
    def _noop(*_a, **_kw):            # → 간단한 콘솔 출력으로 대체
        pass
    def send_discord_debug(msg, *_):
        print(f"[DEBUG][stub] {msg}")
    def send_discord_message(msg, *_):
        print(f"[MSG][stub] {msg}")
import math
from gate_api import (
    ApiClient,
    Configuration,
    FuturesApi,
    FuturesOrder,
    FuturesPriceTriggeredOrder,   # ✅ 선물용 트리거 주문 모델
    ApiException,
)
from gate_api.models import FuturesPriceTrigger

# helper: safe float
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

load_dotenv()

config = Configuration(
    key=os.getenv("GATEIO_API_KEY"),
    secret=os.getenv("GATEIO_API_SECRET"),
    # ✅ 공식 선물 REST 엔드포인트(레거시와 URL 다름)
    host="https://fx-api.gateio.ws/api/v4"
)

# 선물 API 전역 인스턴스
api_client = ApiClient(config)
futures_api = FuturesApi(api_client)

# ─────────────────────────────────────────
# 계약 메타데이터 캐싱(심볼 유효성·스텝 확인용)
# ─────────────────────────────────────────
CONTRACT_CACHE = {}

# ─────────────── 테스트용 출력 ───────────────
# 실행 파일이 import 될 때 돌지 않도록 가드
if __name__ == "__main__":
    for _ in range(6):
        pos = futures_api.list_positions("usdt", holding=True)
        print(json.dumps([p.to_dict() for p in pos if p.contract == "XRP_USDT"],
                         indent=2, ensure_ascii=False))
        sleep(1)

def _ensure_contract_cache():
    global CONTRACT_CACHE
    if not CONTRACT_CACHE:
        contracts = futures_api.list_futures_contracts(settle="usdt")
        CONTRACT_CACHE = {c.name: c for c in contracts}

_ensure_contract_cache()

def set_leverage(symbol: str, leverage: int):
    try:
        contract_symbol = normalize_contract_symbol(symbol)
        try:
            # ✅ 단일모드(One-Way)
            futures_api.update_position_leverage(settle="usdt", contract=contract_symbol, leverage=leverage)
        except Exception:
            # ✅ 듀얼모드(헤지)
            futures_api.update_dual_mode_position_leverage(
                settle="usdt", contract=contract_symbol, leverage=leverage
            )
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

def get_open_position(symbol: str, retry: int = 30, delay: float = 1.0):
    """단일모드 + 듀얼모드 계정 모두 지원"""
    contract_symbol = normalize_contract_symbol(symbol)
    for attempt in range(retry):
        try:
            pos = futures_api.get_position(settle="usdt", contract=contract_symbol)
            if pos:
                size = _f(pos.size)
                entry = _f(pos.entry_price)
                if abs(size) > 0 and entry > 0:
                    print(f"[INFO] 단일 포지션 확인 성공: size={size}, entry={entry}")
                    return {
                        "symbol": symbol,
                        "direction": "long" if size > 0 else "short",
                        "entry": entry,
                        "size": abs(size),
                    }
        except Exception as e:
            print(f"[RETRY-{attempt+1}] get_position 오류: {e}")

        try:
            all_pos = futures_api.list_positions(settle="usdt", holding=True)
            for p in all_pos:
                if p.contract != contract_symbol:
                    continue
                sz = _f(p.size)
                entry_price = _f(p.entry_price)
                d_side = (getattr(p, "dual_side", "") or "").lower()
                if sz != 0:
                    if entry_price > 0 and d_side in ("long", "short"):
                        print(f"[INFO] 듀얼 포지션 확인 성공: side={d_side}, size={sz}, entry={entry_price}")
                        return {
                            "symbol": symbol,
                            "direction": "long" if d_side == "long" else "short",
                            "entry": entry_price,
                            "size": sz,
                        }
                    elif attempt >= 10:
                        # ⏱ fallback: entry_price가 0이어도 포지션 유지되면 리턴
                        print(f"[WARN] 듀얼 포지션 fallback: entry=0, size={sz}, attempt={attempt}")
                        return {
                            "symbol": symbol,
                            "direction": "long" if d_side == "long" else "short",
                            "entry": 0.0,
                            "size": sz,
                        }
        except Exception as e:
            print(f"[RETRY-{attempt+1}] list_positions 오류: {e}")

        if attempt == 0:
            sleep(1.0)
        sleep(delay)

    
# 사용 가능 잔고 조회 (USDT 기준)
def get_available_balance() -> float:
    """통화별 배열 반환 형식 대응(USDT 선택)."""
    try:
        accounts = futures_api.list_futures_accounts(settle="usdt")
        acc = next(a for a in accounts if a.currency.upper() == "USDT")
        return float(acc.available)
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
    contract = CONTRACT_CACHE[normalize_contract_symbol(symbol)]
    step = float(getattr(contract, "size_increment", contract.order_size_min))
    return abs(int(round(-1 * math.log10(step))))

def normalize_contract_symbol(symbol: str) -> str:
    # 이미 '_USDT' 형식이면 그대로 둔다
    if symbol.endswith("_USDT"):
        normalized = symbol
    else:
        normalized = symbol.replace("USDT", "_USDT")
    if normalized not in CONTRACT_CACHE:
        raise ValueError(f"❌ 지원되지 않는 Gate 심볼: {symbol}")
    return normalized

# TP/SL 포함 주문
def place_order_with_tp_sl(symbol: str, side: str, size: float, tp: float, sl: float, leverage: int = 20):
    tick = get_tick_size(symbol)
    tp = float(Decimal(str(tp)).quantize(tick))
    sl = float(Decimal(str(sl)).quantize(tick))
    set_leverage(symbol, leverage)
    contract = normalize_contract_symbol(symbol)

    try:
        # 진입 주문
        entry_order = FuturesOrder(
            contract=contract,
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

        # 포지션 체결 대기
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
            print(f"[INFO] fallback (no pos): fill_price={entry_price} 사용")
        elif pos.get("entry", 0.0) == 0.0:
            print(f"[WARNING] entry=0, 주문 fill_price 기반 TP/SL 계산")
            confirmed_size = abs(float(pos.get("size", size)))
            entry_price = float(entry_res.fill_price or 0.0)
            direction = pos["direction"]
            print(f"[INFO] fallback (entry=0): fill_price={entry_price}, direction={direction}")
        else:
            confirmed_size = abs(float(pos.get("size", size)))
            entry_price = float(pos["entry"])
            direction = pos["direction"]

        if not entry_price:
            raise ValueError("❌ fill_price 없음 → TP/SL 계산 불가")

        # SL/TP 수량 계산
        step_size = float(getattr(CONTRACT_CACHE[contract], "size_increment", getattr(CONTRACT_CACHE[contract], "order_size_min", 1)))
        tp_steps = max(1, math.floor((confirmed_size / 2) / step_size))
        tp_size = tp_steps * step_size
        sl_size = math.floor(confirmed_size / step_size) * step_size

        # TP 지정가 주문
        tp_order = FuturesOrder(
            contract=contract,
            size=int(-tp_size) if side == "buy" else int(tp_size),
            price=str(tp),
            tif="gtc",
            reduce_only=True,
            text="t-TP-SMC"
        )

        tp_res = futures_api.create_futures_order(settle='usdt', futures_order=tp_order)

        if not tp_res:
            raise Exception("TP 주문 실패")

        # SL 트리거 주문 구성 (gate-api 6.97.2 대응)
        sl_trigger = FuturesPriceTriggeredOrder(
            trigger=FuturesPriceTrigger(
                price=str(sl),
                rule=2,
                price_type=1,
                expiration=86400
            ),
            initial=FuturesOrder(
                contract=contract,
                size=int(-sl_size) if side == "buy" else int(sl_size),
                price="0",
                tif="ioc",
                reduce_only=True,
                text="t-SL-SMC"
            )
        )

        sl_res = futures_api.create_price_triggered_order(
            settle="usdt", futures_price_triggered_order=sl_trigger
        )

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

        # ✅ SL 갱신(취소 → 재생성)
        trigger = FuturesPriceTriggeredOrder()
        trigger.contract      = contract
        trigger.size          = int(size) if direction == "long" else int(-size)
        trigger.trigger_price = str(normalized_stop)
        trigger.price_type    = 1
        trigger.close         = True
        trigger.text          = "t-SL-UPDATE"
        trigger.order_price   = "0"
        trigger.tif           = "ioc"
        trigger.reduce_only   = True

        futures_api.create_price_triggered_order(settle="usdt", price_triggered_order=trigger)

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
        contract = CONTRACT_CACHE[normalize_contract_symbol(symbol)]
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
        contract = CONTRACT_CACHE[contract_symbol]
        step_size = float(contract.size_increment)  # ✅
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
    
TICK_CACHE = {}

def get_tick_size_gate(symbol: str) -> float:
    if symbol in TICK_CACHE:
        return TICK_CACHE[symbol]
    contract = CONTRACT_CACHE[normalize_contract_symbol(symbol)]
    TICK_CACHE[symbol] = float(contract.tick_size)
    return TICK_CACHE[symbol]
