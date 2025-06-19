# exchange/gate_sdk.py

import json
import os
from time import time, sleep
from decimal import Decimal
from config.settings import TRADE_RISK_PCT
import requests
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
    FuturesPriceTriggeredOrder,    # ▶ SL·TP 트리거 주문 모델
    ApiException,
)
from gate_api.exceptions import ApiException
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

# quiet=True ⇒ 성공 로그 생략, 실패만 경고
def set_leverage(symbol: str, leverage: int, *, quiet: bool = False):
    contract_symbol = normalize_contract_symbol(symbol)
    try:
        # SDK 6.97.x → 세 번째 인자에 정수 레버리지 직접 전달
        futures_api.update_position_leverage("usdt", contract_symbol, leverage)
    except ApiException as e:
        if "dual mode" in str(e).lower():
            futures_api.update_dual_mode_position_leverage(
                "usdt", contract_symbol, leverage
            )
        else:
            raise e
    if not quiet:
        msg = f"[GATE] 레버리지 설정 완료: {symbol} → x{leverage}"
        print(msg)
        send_discord_debug(msg, "gateio")
    return True

def place_order(symbol: str, side: str, size: float, leverage: int = 20, **_kw):
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

def get_open_position(symbol: str, max_wait: float = 15.0, delay: float = 0.5):
    contract_symbol = normalize_contract_symbol(symbol)
    t0 = time()

    # max_wait ≤ 0  → 단일 조회(논블로킹)
    first_only = max_wait <= 0
    while True:
        try:
            # 단일 포지션 조회
            pos = futures_api.get_position(settle="usdt", contract=contract_symbol)
            size = _f(pos.size)
            entry = _f(pos.entry_price)
            mode = getattr(pos, "mode", "").lower()
            if size != 0 and entry > 0:
                direction = "long" if size > 0 else "short"
                print(f"[INFO] 단일 포지션 확인: mode={mode}, size={size}, entry={entry}")
                return {
                    "symbol": symbol,
                    "direction": direction,
                    "entry": entry,
                    "size": abs(size),
                }
        except Exception as e:
            # 포지션이 없으면 바로 중단
            if e.status == 400 and "POSITION_NOT_FOUND" in e.body:
                print(f"[INFO] 포지션 없음 → 즉시 종료: {symbol}")
                return None
            print(f"[RETRY] get_position 실패: {e}")

        try:
            # 듀얼 포지션 탐색
            all_pos = futures_api.list_positions(settle="usdt", holding=True)
            for p in all_pos:
                if p.contract != contract_symbol:
                    continue
                size = _f(p.size)
                entry = _f(p.entry_price)
                mode = (getattr(p, "mode", "") or getattr(p, "dual_side", "")).lower()
                if size and entry and mode:
                    direction = "long" if "long" in mode else "short"
                    print(f"[INFO] 듀얼 포지션 확인: mode={mode}, size={size}, entry={entry}")
                    return {
                        "symbol": symbol,
                        "direction": direction,
                        "entry": entry,
                        "size": abs(size),
                    }
        except Exception as e:
            print(f"[RETRY] list_positions 오류: {e}")

        if first_only:
            break          # 한 번만 시도
        sleep(delay)

    if not first_only:     # 논블로킹일 땐 조용히 패스
        print(f"[TIMEOUT] 포지션 entry_price 확인 실패: {symbol}")
    return None
    
# 사용 가능 잔고 조회 (USDT 기준)
def get_available_balance() -> float:
    """Gate Futures 계정의 사용 가능 USDT 잔고 조회"""
    try:
        # 6.97.2는 **리스트** 반환 → 첫 요소 사용
        acc = futures_api.list_futures_accounts("usdt")
        # SDK 6.97.x → 객체, 6.96 이전 → 리스트  
        if isinstance(acc, list):
            acc = acc[0]
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

# ────────────────────────────────
# Binance 심볼 → Gate 심볼 변환
#   'BTCUSDT' → 'BTC_USDT'
# ────────────────────────────────
def to_gate_symbol(symbol: str) -> str:
    """Binance 형식을 Gate 형식으로 변환 (이미 Gate 형식이면 그대로)."""
    return symbol if symbol.endswith("_USDT") else symbol.replace("USDT", "_USDT")

# 레거시 별칭
to_gate = to_gate_symbol

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

        if not pos or pos.get("entry", 0.0) == 0.0:
            raise ValueError(f"❌ 포지션 조회 실패 또는 entry=0 → TP/SL 설정 중단: {symbol}")
        confirmed_size = abs(float(pos.get("size", size)))
        entry_price = float(pos["entry"])
        direction = pos["direction"]

        # 마크 가격 실시간 조회
        mark_url = f"https://fx-api.gateio.ws/api/v4/futures/usdt/mark_price/{contract}"
        mark_resp = requests.get(mark_url, timeout=3).json()
        mark_price = float(mark_resp["mark_price"]) if "mark_price" in mark_resp else entry_price

        if not entry_price or not mark_price:
            raise ValueError("❌ 가격 정보 부족 → TP/SL 계산 불가")
        
        # ✅ SL 보정 (마크가격·엔트리 기준 안전 확보)
        min_diff = float(tick)
        if direction == "long":
            sl = min(sl, entry_price - min_diff, mark_price - min_diff)
            if sl >= entry_price or sl >= mark_price:
                raise ValueError(f"❌ SL 오류 (롱) → SL={sl}, Entry={entry_price}, Mark={mark_price}")
        elif direction == "short":
            sl = max(sl, entry_price + min_diff, mark_price + min_diff)
            if sl <= entry_price or sl <= mark_price:
                raise ValueError(f"❌ SL 오류 (숏) → SL={sl}, Entry={entry_price}, Mark={mark_price}")

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

        # ─ SL 트리거 주문 구성은 main.py → update_stop_loss() 에서 일괄 관리합니다.
        # 따라서 이곳의 SL 생성 로직은 제거/주석 처리합니다.
        # sl_price = sl * (0.999 if direction == "long" else 1.001)
        # sl_price = float(Decimal(str(sl_price)).quantize(tick))
        # sl_rule  = 2 if direction == "long" else 1
        # sl_trigger = FuturesPriceTriggeredOrder(...)
        # sl_res = futures_api.create_price_triggered_order(...)
        # if not sl_res: raise Exception("SL 주문 실패")

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

        # ▶ 6.97.x 모델 객체로 생성 (API 스펙 완전 준수)
        sl_order = FuturesPriceTriggeredOrder(
            contract    = contract,
            size        = 0,                # close=True 로 전량
            close       = True,
            trigger     = str(normalized_stop),
            price_type  = 0,                # 0 = 최근 체결가
            rule        = 2 if direction == "long" else 1,
            order_type  = "market",
            tif         = "ioc",
            text        = "t-SL-UPDATE",
            type        = 3,                # 3 = stop-loss
        )
        futures_api.create_price_triggered_order("usdt", sl_order)

        msg = f"[SL 갱신] {symbol} SL 재설정 완료 → {normalized_stop}"
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

def calculate_quantity_gate(
    symbol: str,
    price: float,
    usdt_balance: float,
    leverage: int = 10,
    risk_ratio: float = TRADE_RISK_PCT,
) -> float:
    """
    ▸ `risk_ratio` = 사용할 증거금 비율 (예: 0.30 → 30 %)
    ▸ *증거금* = (usdt_balance × risk_ratio)  
      ⇒ 목표 *명목가* = 증거금 × leverage
    """
    try:
        from math import floor
        margin_cap   = usdt_balance * risk_ratio          # 사용할 최대 증거금
        target_notional = margin_cap * leverage            # 목표 명목가
        raw_qty      = target_notional / price
        contract_symbol = normalize_contract_symbol(symbol)  # ✅ Gate 심볼 포맷 변환
        contract = CONTRACT_CACHE[contract_symbol]
        step_size = float(
            getattr(contract, "size_increment", getattr(contract, "order_size_min", 0.1))
        )
        precision = get_contract_precision(symbol)
        steps = floor(raw_qty / step_size)
        max_steps = floor((margin_cap * leverage) / (price * step_size))
        steps = max(1, min(steps, max_steps - 2))  # 초과 방지용 1단계 여유
        qty   = round(steps * step_size, precision)

        if qty < step_size:
            print(f"[GATE] 최소 주문 수량 미달: 계산된 qty={qty}, 최소={step_size}")
            return 0.0

        print(
            f"[GATE] 수량 계산 → raw_qty={raw_qty}, steps={steps}, "
            f"qty={qty}, max_steps={max_steps}, risk_cap={margin_cap}"
        )
        return qty
    
    except Exception as e:
        print(f"[GATE] ❌ 수량 계산 실패: {e}")
        send_discord_debug(f"[GATE] ❌ 수량 계산 실패: {e}", "gateio")
        return 0.0
    
TICK_CACHE: dict[str, float] = {}

def _contract_tick(c) -> float:
    """
    Gate v4 선물 `FuturesContract` 객체는
    - 신규 필드: `tick_size`
    - 구버전  : `order_price_round`
    둘 중 하나만 존재할 수 있어 안전하게 가져온다.
    """
    tick = getattr(c, "tick_size", None)
    if not tick or float(tick) == 0:        # 없거나 0 → fallback
        tick = getattr(c, "order_price_round", "0.0001")
    return float(tick)

def get_tick_size_gate(symbol: str) -> float:
    if symbol in TICK_CACHE:
        return TICK_CACHE[symbol]
    c = CONTRACT_CACHE[normalize_contract_symbol(symbol)]
    # SDK 6.98 이후 tick_size → order_price_round 로 변경
    val = getattr(c, "tick_size", None) or getattr(c, "order_price_round", "0.0001")
    TICK_CACHE[symbol] = float(val)
    return TICK_CACHE[symbol]

# ─────────────────────────────────────────────────────────────
#  NEW : TP(LIMIT) 주문 갱신/재발주  ★
# ─────────────────────────────────────────────────────────────
def update_take_profit_order(symbol: str, direction: str, take_price: float):
    """
    Gate.io USDT-Futures  
    ▸ 기존 reduce-only LIMIT(TP) 주문 취소 후 새로 발행  
    ▸ 가격은 tickSize 라운딩
    """
    try:
        contract  = normalize_contract_symbol(symbol)
        tick_dec  = get_tick_size(symbol)
        tp_dec    = Decimal(str(take_price)).quantize(tick_dec)
        tp_price  = str(tp_dec)

        # 현 포지션 조회 (없으면 실패)
        pos = get_open_position(symbol)
        if not pos:
            return False
        qty_full = float(pos["size"])
        qty_half = qty_full / 2

        # 수량 스텝 반올림
        step   = float(getattr(CONTRACT_CACHE[contract],
                               "size_increment",
                               getattr(CONTRACT_CACHE[contract], "order_size_min", 1)))
        qty_tp = max(step, math.floor(qty_half / step) * step)
        side_sz = -qty_tp if direction == "long" else qty_tp

        # ① 기존 TP 주문 취소
        try:
            for od in futures_api.list_orders(settle="usdt",
                                              contract=contract,
                                              status="open"):
                if od.reduce_only and od.type == "limit":
                    # 6.97.x ⇒ 3-번째 인자는 order_id (키워드 X)
                    futures_api.cancel_orders(
                        "usdt",            # settle
                        contract,          # contract
                        od.id              # order_id
                    )
        except Exception:
            pass

        # ② 새 TP 트리거 주문 (모델 객체)
        tp_order = FuturesPriceTriggeredOrder(
            contract    = contract,
            size        = 0,
            close       = True,
            trigger     = tp_price,
            price_type  = 0,
            rule        = 1 if direction == "long" else 2,
            order_type  = "market",
            tif         = "ioc",
            text        = "t-TP-UPDATE",
            type        = 2,      # 2 = take-profit
        )
        futures_api.create_price_triggered_order("usdt", tp_order)
        print(f"[TP 갱신] {symbol} TP 트리거 재설정 완료 → {tp_price}")
        send_discord_debug(f"[TP 갱신] {symbol} TP 트리거 재설정 완료 → {tp_price}", "gateio")
        return True

    except Exception as e:
        print(f"[ERROR] TP 갱신 실패: {symbol} → {e}")
        send_discord_debug(f"[ERROR] TP 갱신 실패: {symbol} → {e}", "gateio")
        return False