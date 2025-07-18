# exchange/gate_sdk.py

import json
import os
from time import time, sleep
from decimal import Decimal, ROUND_UP, ROUND_DOWN
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
CONTRACT_CACHE: dict[str, object] = {}
# Gate USDT-선물 기본 최소 명목가
MIN_USDT_NOTIONAL = 5.0

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
    # ▸ 가격 라운딩 (Binance 방식과 동일)
    tick = get_tick_size(symbol)
    if side == "buy":                       # LONG
        tp_dec = Decimal(str(tp)).quantize(tick, ROUND_UP)
        sl_dec = Decimal(str(sl)).quantize(tick, ROUND_DOWN)
    else:                                   # SHORT
        tp_dec = Decimal(str(tp)).quantize(tick, ROUND_DOWN)
        sl_dec = Decimal(str(sl)).quantize(tick, ROUND_UP)
    tp = float(tp_dec); sl = float(sl_dec)
    # ────────── sanity check ──────────
    if tp == 0 or sl == 0:
        raise ValueError(f"[ABORT] 잘못된 TP/SL 계산 → tp={tp}, sl={sl}")
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

        # ────── TP 수량 : 절반 (stepSize 미달 → 전량) ──────
        step_size = float(getattr(CONTRACT_CACHE[contract],
                                  "size_increment",
                                  getattr(CONTRACT_CACHE[contract], "order_size_min", 1)))
        tp_size_raw = math.floor((confirmed_size / 2) / step_size) * step_size
        tp_size = tp_size_raw if tp_size_raw >= step_size else confirmed_size
        sl_size = math.floor(confirmed_size / step_size) * step_size

        # ── (A) 기존 reduce-only LIMIT 주문 전량 취소 ────────────
        try:
            for od in futures_api.list_orders(settle="usdt",
                                              contract=contract,
                                              status="open"):
                if od.reduce_only and od.type == "limit":
                    futures_api.cancel_orders("usdt", contract, od.id)
        except Exception:
            pass
        # ── (B) 새 TP 지정가 주문 발행 ────────────────────────────
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

        # 간단한 진입 알림만 전송 (상세 정보는 main.py에서 처리)
        msg = f"[TP/SL] {symbol} 진입 및 TP/SL 설정 완료 → TP: {tp}, SL: {sl}"
        print(msg)
        # send_discord_message는 main.py에서 상세 정보와 함께 전송
        return True

    except Exception as e:
        msg = f"[ERROR] TP/SL 포함 주문 실패: {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "gateio")
        return False
        
def update_stop_loss_order(symbol: str, direction: str, stop_price: float):
    try:
        contract = normalize_contract_symbol(symbol)
        # ── (0) 보유 포지션 체크 ───────────────────────
        pos = futures_api.get_position(settle="usdt", contract=contract)
        if float(pos.size) == 0:
            # ▸ 포지션이 없으면 남아있는 SL 트리거만 정리하고
            #   True 로 반환해 상위 로직이 강제 종료를 호출하지 않도록 한다.
            try:
                for od in futures_api.list_price_triggered_orders(
                        "usdt", contract=contract, status="open"):
                    futures_api.cancel_price_triggered_orders("usdt", od.id)
            except Exception:
                pass
            return True

        tick = get_tick_size(symbol)
        # ── (1) stop_price 안전 보정 (Mark ± 1 tick) ──
        # ① markPrice – 실패가 잦아 → 다중 폴백
        mark = 0.0
        try:                                               # ① REST /mark_price
            rj   = requests.get(
                    f"https://fx-api.gateio.ws/api/v4/futures/usdt/mark_price/{contract}",
                    timeout=3
                  ).json()
            mark = float(rj.get("mark_price") or rj.get("price", 0))
        except Exception:
            pass
        if not mark:                                       # ② 24h ticker
            try:
                tkr  = futures_api.list_futures_tickers("usdt", contract=contract)[0]
                mark = float(getattr(tkr, "last", 0))
            except Exception:
                pass
        if not mark:                                       # ③ entryPrice fallback
            mark = float(pos.entry_price)
        raw  = Decimal(str(stop_price)).quantize(tick)
        tick_f = float(tick)
        if direction == "long"  and raw >= Decimal(str(mark)):
            raw = Decimal(str(mark - tick_f)).quantize(tick)
        if direction == "short" and raw <= Decimal(str(mark)):
            raw = Decimal(str(mark + tick_f)).quantize(tick)
        normalized_stop = float(raw)

        # ── (2) 현재 열려있는 SL 트리거를 먼저 조회
        open_triggers = futures_api.list_price_triggered_orders(
            "usdt", contract=contract, status="open"
        )

        # ── (2-a) 동일 가격·규칙의 SL 이미 존재하면 재발행 생략 ── ★ NEW
        for od in open_triggers:
            try:
                trg_price = float(od.trigger.get("price"))
                rule      = int(od.trigger.get("rule"))
                want_rule = 2 if direction == "long" else 1
                if abs(trg_price - normalized_stop) < float(tick) and rule == want_rule:
                    return True       # 중복 방지: 그대로 유지
            except Exception:
                pass

        # ── (3) 새 SL 단일 발행  (Binance STOP_MARKET 대응) ────
        # Gate v4: initial 쪽에 order_type/close 대신
        #   - reduce_only = True
        #   - price      = "0"
        sl_order = FuturesPriceTriggeredOrder(
            initial={
                "contract": contract,
                "size": 0,          # 전량 청산
                "price": "0",       # 시장가
                "close": True,      # ★ size 0 이면 필수!
                "order_type": "market",
                "tif": "ioc",
                "text": "t-SL-UPDATE",
            },
            trigger={
                "price_type": 1,                       # 0=last, 1=mark, 2=index
                "price": str(normalized_stop),
                "rule": 2 if direction == "long" else 1,
            }
        )
        try:
            new_sl = futures_api.create_price_triggered_order("usdt", sl_order)
        except Exception as e:
            # ▸ 새 SL 실패 → 기존 SL 유지, 강제 종료 방지
            send_discord_debug(
                f"[SL-FAIL] {symbol} 새 SL 생성 실패, 기존 SL 유지 → {e}",
                "gateio"
            )
            return True   # 실패해도 True 반환하여 close_position() 차단

        # ── (4) 새 SL 성공했으면 이전 SL 취소 ─────────────────
        for od in open_triggers:
            try:
                futures_api.cancel_price_triggered_orders("usdt", od.id)
            except Exception:
                pass

        msg = (
            f"[SL 갱신] {symbol} SL 재설정 완료 → {normalized_stop} "
            f"(id={getattr(new_sl,'id','?')})"
        )
        print(msg)
        send_discord_debug(msg, "gateio")
        return True
    except Exception as e:
        msg = f"[ERROR] SL 갱신 실패: {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "gateio")
        return False

# ─────────────────────────────────────────────────────────────
#  NEW : router.cancel_order() 가 호출하는 “트리거 취소” 헬퍼 ★
# ─────────────────────────────────────────────────────────────
def cancel_price_trigger(order_id: int | str) -> bool:
    """
    SL/TP price-triggered 주문 하나만 취소  
    (router → cancel_order 경유)
    """
    try:
        futures_api.cancel_price_triggered_orders("usdt", order_id)
        print(f"[GATE] price_triggered_order 취소 완료 | id={order_id}")
        return True
    except Exception as e:
        send_discord_debug(f"[GATE] ❌ price_trigger 취소 실패(id={order_id}) → {e}", "gateio")
        return False

def close_position(symbol: str):
    try:
        contract = normalize_contract_symbol(symbol)
        pos = futures_api.get_position(settle='usdt', contract=contract)
        size = float(pos.size)
        if size == 0:
            return False

        futures_api.create_futures_order(
            settle='usdt',
            futures_order=FuturesOrder(
                contract=contract,
                size=-size,
                price="0",
                tif="ioc",
                reduce_only=True,
                text="t-FORCE-CLOSE",
            )
        )
        
        print(f"[GATE] 포지션 강제 종료 완료 | {symbol}")
        send_discord_debug(f"[GATE] 포지션 강제 종료 완료 | {symbol}", "gateio")
        return True
    except Exception as e:
        msg = f"[GATE] ❌ 포지션 종료 실패 | {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "gateio")
        return False

def get_tick_size(symbol: str) -> Decimal:
    """`tick_size` 우선, 없으면 `order_price_round` 사용"""
    try:
        contract = CONTRACT_CACHE[normalize_contract_symbol(symbol)]
        tick = getattr(contract, "tick_size", None) or getattr(contract, "order_price_round", "0.0001")
        return Decimal(str(tick)).normalize()       # ← 0.010000 → 0.01
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
        # ────── 목표 수량(계약 수) 계산 ──────
        contract_symbol = normalize_contract_symbol(symbol)  # ✅ Gate 심볼
        contract        = CONTRACT_CACHE[contract_symbol]
        multiplier      = float(getattr(contract, "quanto_multiplier", 1) or 1)
        contract_val    = price * multiplier              # 1계약 명목가
        raw_qty         = target_notional / contract_val
        step_size = float(
            getattr(contract, "size_increment",
                    getattr(contract, "order_size_min", 0.1))
        )
        # (추가) **MIN_NOTIONAL** 유사 보정
        # 일부 코인(신규 상장·저가)은 상품 메타에 min_notional 이 없음
        # → Gate 기본 5 USDT 를 fallback 으로 사용
        min_notional_cfg = float(getattr(contract, "min_notional", 0) or 0)
        size_max  = float(getattr(contract, "order_size_max", 0)) or None
        precision = get_contract_precision(symbol)
        steps = floor(raw_qty / step_size)
        # 최대 가능 스텝(노셔널 기준, 여유 95 %)
        max_steps_notional = floor((margin_cap * 0.95 * leverage)
                                   / (contract_val * step_size))
        if max_steps_notional <= 0:            # 증거금 부족 → 바로 종료
            print(f"[GATE] ❌ 증거금 부족: price={price}, step={step_size}, "
                  f"cap={margin_cap}, lev={leverage}")
            return 0.0
        # 거래소 절대 수량 한도도 함께 고려
        if size_max:
            max_steps_limit = floor(size_max / step_size)
            max_steps = min(max_steps_notional, max_steps_limit)
        else:
            max_steps = max_steps_notional

        steps = max(1, min(steps, max_steps))

        # 유지증거금 부족(LIQUIDATE_IMMEDIATELY) 방지용
        #   ⇒ 예상 필요 증거금 계산 후 부족-분기 추가 
        est_margin = (steps * step_size * contract_val) / leverage
        if est_margin > margin_cap:
            steps = floor((margin_cap * 0.95 * leverage)
                          / (contract_val * step_size))
            steps = max(1, steps)

        # ────── 절반 익절 고려 최소 포지션 사이즈 보장 ──────
        # 절반 익절 후에도 의미 있는 물량이 남도록 최소 4 step 보장
        min_steps_for_half_exit = 4
        if steps < min_steps_for_half_exit:
            # 증거금 여유가 있다면 최소 사이즈로 조정
            needed_margin = (min_steps_for_half_exit * step_size * contract_val) / leverage
            if needed_margin <= margin_cap * 0.9:  # 10% 여유 두고 확인
                steps = min_steps_for_half_exit
                print(f"[GATE] 절반 익절 고려 최소 사이즈 적용: {steps} steps")
            else:
                print(f"[GATE] ⚠️ 절반 익절 고려 시 증거금 부족: steps={steps}")

        qty   = round(steps * step_size, precision)

        # ──────────────────────────────────────────────────────
        # ❶ notional(명목가) 부족 시 stepSize 단위로 증가
        #    - min_notional이 없으면 MIN_USDT_NOTIONAL(5) 적용
        # ❷ margin(증거금) 한계를 넘으면 0 으로 drop → 주문 스킵
        # ──────────────────────────────────────────────────────
        min_notional_req = max(min_notional_cfg, MIN_USDT_NOTIONAL)
        while qty * price < min_notional_req:
            steps += 1
            qty = round(steps * step_size, precision)
            est_margin = (steps * step_size * contract_val) / leverage
            if est_margin > margin_cap * 0.95:     # 증거금 초과 시 중단
                qty = 0.0
                break

        # 최종 sanity-check
        if qty < step_size or qty * price < min_notional_req:
            print(
                f"[GATE] 주문 최소값 미달 → qty={qty}, "
                f"notional={qty*price:.4f} < {min_notional_req}"
            )
            return 0.0

        # ────── 절반 익절 후 물량 검증 ──────────────────────────────
        # 절반 익절 후 남은 물량이 의미 있는지 미리 확인
        remaining_after_half = qty - math.floor((qty / 2) / step_size) * step_size
        if remaining_after_half < step_size:
            print(f"[GATE] ⚠️ 절반 익절 후 남은 물량 부족: {remaining_after_half} < {step_size}")
            print(f"[GATE] ⚠️ 권장: 더 큰 포지션 사이즈 또는 다른 심볼 고려")
        
        print(
            f"[GATE] 수량 계산 → raw_qty={raw_qty}, steps={steps}, "
            f"qty={qty}, max_steps={max_steps}, "
            f"min_notional={min_notional_req}, "
            f"risk_cap={margin_cap}, est_margin={est_margin}, "
            f"half_exit_remaining={remaining_after_half}"
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
        # Binance 와 동일한 라운딩 규칙 적용
        if direction == "long":
            tp_dec = Decimal(str(take_price)).quantize(tick_dec, ROUND_UP)
        else:
            tp_dec = Decimal(str(take_price)).quantize(tick_dec, ROUND_DOWN)
        tp_price  = str(tp_dec)

        # 현 포지션 조회 (없으면 실패)
        pos = get_open_position(symbol)
        if not pos:
            return False
        qty_full = float(pos["size"])
        # ── (1) 수량 : **절반 익절** ──────────────────────────────
        qty_half = qty_full / 2
        step     = float(getattr(CONTRACT_CACHE[contract],
                                 "size_increment",
                                 getattr(CONTRACT_CACHE[contract], "order_size_min", 1)))
        qty_tp_raw = math.floor(qty_half / step) * step
        
        # ────── 절반 익절 로직 (단순화) ──────────────────────────────
        # 정확한 절반 익절만 수행
        if qty_tp_raw >= step:
            qty_tp = qty_tp_raw
            remaining_qty = qty_full - qty_tp
            print(f"[GATE] 절반 익절: {qty_tp}/{qty_full} (남은 물량: {remaining_qty})")
        else:
            # 절반 익절이 stepSize보다 작으면 전량 TP
            qty_tp = qty_full
            print(f"[GATE] ⚠️ 전량 익절 (절반이 stepSize 미달): {qty_tp}/{qty_full}")
        
        # 최종 검증: stepSize 미달이면 전량 TP
        if qty_tp < step:
            qty_tp = qty_full
            print(f"[GATE] ⚠️ stepSize 미달로 전량 익절: {qty_tp}/{qty_full}")

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

        # ② 새 TP LIMIT 주문 (reduce-only, 절반 물량)
        tp_order = FuturesOrder(
            contract    = contract,
            size        = -qty_tp if direction == "long" else  qty_tp,
            price       = tp_price,
            tif         = "gtc",
            reduce_only = True,
            text        = "t-TP-UPDATE",
        )
        futures_api.create_futures_order(settle="usdt", futures_order=tp_order)
        print(f"[TP 갱신] {symbol} LIMIT TP 재설정 완료 → {tp_price} (qty={qty_tp})")
        send_discord_debug(f"[TP 갱신] {symbol} LIMIT TP 재설정 완료 → {tp_price}", "gateio")
        return True

    except Exception as e:
        print(f"[ERROR] TP 갱신 실패: {symbol} → {e}")
        send_discord_debug(f"[ERROR] TP 갱신 실패: {symbol} → {e}", "gateio")
        return False

def verify_sl_exists_gate(symbol: str, expected_sl_price: float = None) -> bool:
    """
    Gate.io에서 심볼의 SL 주문이 실제로 존재하는지 확인
    Args:
        symbol: 심볼명 (Gate 형식: BTC_USDT)
        expected_sl_price: 예상 SL 가격 (선택사항)
    Returns:
        bool: SL 주문 존재 여부
    """
    try:
        # Gate에서는 trigger 주문으로 SL 관리
        open_triggers = futures_api.list_price_triggered_orders("usdt", status="open")
        
        # 해당 심볼의 SL 주문 찾기
        sl_orders = []
        for order in open_triggers:
            if (order.initial.contract == symbol and 
                order.initial.close == True and  # 청산 주문
                order.trigger.rule in [1, 2]):  # 1=short SL, 2=long SL
                sl_orders.append(order)
        
        if not sl_orders:
            return False
            
        if expected_sl_price is not None:
            tick = get_tick_size_gate(symbol)
            for order in sl_orders:
                trigger_price = float(order.trigger.price)
                if abs(trigger_price - expected_sl_price) < float(tick):
                    return True
            return False
            
        return True
        
    except Exception as e:
        print(f"[ERROR] Gate SL 검증 실패: {symbol} → {e}")
        return False

def ensure_stop_loss_gate(symbol: str, direction: str, sl_price: float, max_retries: int = 3) -> bool:
    """
    Gate.io에서 SL 주문이 확실히 존재하도록 보장
    Args:
        symbol: 심볼명 (Gate 형식)
        direction: 방향 (long/short)
        sl_price: SL 가격
        max_retries: 최대 재시도 횟수
    Returns:
        bool: SL 설정 성공 여부
    """
    import time
    from notify.discord import send_discord_debug
    
    for attempt in range(max_retries):
        # 1. 현재 SL 존재 여부 확인
        if verify_sl_exists_gate(symbol, sl_price):
            print(f"[SL] {symbol} Gate SL 주문 확인됨 @ {sl_price:.4f}")
            return True
        
        # 2. SL 주문 생성/업데이트 시도
        print(f"[SL] {symbol} Gate SL 주문 생성 시도 {attempt + 1}/{max_retries}")
        success = update_stop_loss_order(symbol, direction, sl_price)
        
        if success:
            time.sleep(1)  # 주문 반영 대기
            if verify_sl_exists_gate(symbol, sl_price):
                print(f"[SL] {symbol} Gate SL 주문 생성 성공 @ {sl_price:.4f}")
                return True
        
        # 3. 재시도 대기 (지수 백오프)
        if attempt < max_retries - 1:
            wait_time = 2 ** attempt
            print(f"[SL] {symbol} Gate SL 설정 실패 - {wait_time}초 후 재시도")
            time.sleep(wait_time)
    
    # 4. 최종 실패 시 알림
    error_msg = f"[CRITICAL] {symbol} Gate SL 설정 최종 실패 - 수동 확인 필요!"
    print(error_msg)
    send_discord_debug(error_msg, "gateio")
    return False