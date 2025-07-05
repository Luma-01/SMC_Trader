# exchange/router.py

# ───────── Binance ─────────
from exchange.binance_api import (
    update_stop_loss_order as binance_sl,
    update_take_profit_order as binance_tp,      # ★ NEW
    get_open_position       as binance_pos,
    place_order             as binance_place,
)
# ───────── Gate ───────────
from exchange.gate_sdk import (
    get_open_position         as gate_pos,
    update_stop_loss_order    as gate_sl,
    update_take_profit_order  as gate_tp,        # ★ NEW
    normalize_contract_symbol as to_gate,
    place_order               as gate_place,
)
# ── 표준 라이브러리 ─────────────────────────────
from decimal import Decimal

# ------------------------------------------------------------------
#  tickSize  통합 랩퍼  (Binance / Gate 공용)  ―  lazy-import 로 순환 차단
# ------------------------------------------------------------------
def get_tick_size(symbol: str) -> float:
    """
    Binance :  BTCUSDT
    Gate    :  BTC_USDT
    """
    try:
        if symbol.endswith("_USDT"):
            # Gate 심볼 → gate_sdk 만 **지연 import**
            from exchange.gate_sdk import get_tick_size as _gate_tick
            return float(_gate_tick(symbol))
        # Binance
        from exchange.binance_api import get_tick_size as _bin_tick
        return float(_bin_tick(symbol.replace("_", "")))
    except Exception:
        return 0.0
# Discord 로깅 (SL/TP·포지션 오류 알림용)  ★ NEW
from notify.discord import send_discord_debug
# Gate 심볼 집합(BTC_USDT 형식) 생성 (미지원 심볼 스킵)
from config.settings import SYMBOLS_GATE
GATE_SET = set()
for sym in SYMBOLS_GATE:
    try:
        GATE_SET.add(to_gate(sym))
    except ValueError as e:
        # 콘솔에 경고. 필요시 send_discord_debug 로 대체 가능
        print(f"[WARN] Gate 심볼 변환 실패, 스킵: {sym} ({e})")

def update_stop_loss(symbol: str, direction: str, stop_price: float):
    """
    symbol 예시
      - Binance : BTCUSDT
      - Gate    : BTC_USDT  ← 이미 변환된 값
    """
    print(f"[router] SL 갱신 요청: {symbol} → {stop_price}")

    # ────────────────────────────────────────────────
    #   ▶ 현재 “오픈 주문” 중 STOP-MARKET 이 있는지 살펴보고
    #     stopPrice 가 변동 없으면 재발주하지 않음
    # ────────────────────────────────────────────────

    def _current_sl_price(sym: str) -> float | None:
        try:
            if sym in GATE_SET:                 # ── Gate
                from exchange.gate_sdk import get_open_orders
                for o in get_open_orders(sym):
                    if o.get("type") == "trigger" and o.get("reduce_only"):
                        return float(o["price"])
            else:                               # ── Binance
                from exchange.binance_api import client, ORDER_TYPE_STOP_MARKET
                b_sym = sym.replace("_", "")
                for o in client.futures_get_open_orders(symbol=b_sym):
                    if o["type"] == ORDER_TYPE_STOP_MARKET and (
                        o.get("reduceOnly") or o.get("closePosition")
                    ):
                        return float(o["stopPrice"])
        except Exception as e:
            print(f"[router] SL 가격 조회 실패({sym}) → {e}")
        return None

    tick = get_tick_size(symbol)
    cur_sl = _current_sl_price(symbol)
    if cur_sl is not None and abs(cur_sl - stop_price) < float(tick):
        # ±1 tick 이내면 동일 주문으로 간주 → no-op
        return True
    if symbol in GATE_SET:       # Gate 심볼이면
        return gate_sl(symbol, direction, stop_price)
    return binance_sl(symbol, direction, stop_price)

# ==========================================================
#   NEW : TP(리미트) 가격 수정 라우터
# ==========================================================
def update_take_profit(symbol: str, direction: str, take_price: float):
    """
    ▸ 이미 존재하는 TP 리미트 주문 가격을 수정  
    ▸ 없는 경우 새 주문을 생성한다  
      - Binance : `update_take_profit_order()` 사용  
      - Gate    : reduce-only LIMIT 주문 재발주 방식
    """
    print(f"[router] TP 갱신 요청: {symbol} → {take_price}")
    try:
        # ① tickSize 라운드(거래소별 함수에서도 재확인하지만 1차 보정) ★
        tick = get_tick_size(symbol)
        take_price = float(Decimal(str(take_price)).quantize(Decimal(str(tick))))

        # ② 거래소별 TP 갱신 함수 호출
        if symbol in GATE_SET:
            return gate_tp(symbol, direction, take_price)
        return binance_tp(symbol, direction, take_price)
    except Exception as e:
        print(f"[router] TP 갱신 실패: {e}")
        return False
    
def cancel_order(symbol: str, order_id: int):
    """
    Gate:  ❯ price_triggered_order 를 **ID 로 직접 취소**
           (더 이상 포지션을 강제 종료하지 않음)
    Binance: 기존 로직 유지
    """
    if "_USDT" in symbol:
        from exchange.gate_sdk import cancel_price_trigger      # ★ NEW
        return cancel_price_trigger(order_id)

    from exchange.binance_api import cancel_order as binance_cancel_order
    try:
        # Binance: 정상적으로 취소되면 True 반환
        return binance_cancel_order(symbol, order_id)
    except Exception as e:
        # -2011: Unknown order sent   /   -1102: orderId 누락·오류
        # ↳ 이미 체결‧취소된 주문을 다시 지우려 할 때 흔히 발생
        if any(code in str(e) for code in ("-2011", "-1102")):
            # benign → False 반환해 상위 로직이 “이미 없어졌다”로 간주
            return False
        raise          # 그 외 에러는 그대로 올려서 디버그

def get_open_position(symbol: str, *args, **kwargs):
    """
    통합 포지션 조회 헬퍼

    ▸ Gate `get_open_position()` 은 (symbol, max_wait=…, delay=…) 형태를 지원합니다.  
    ▸ Binance 버전은 (symbol) 하나만 받으므로, 전달된 추가 인자는 **무시**합니다.
    """
    try:
        if "_USDT" in symbol:                       # Gate 선물 심볼
            return gate_pos(symbol, *args, **kwargs)
        # Binance 심볼 → 여분 인자는 사용하지 않음
        return binance_pos(symbol)

    except Exception as e:
        exch = "Gate" if "_USDT" in symbol else "Binance"
        msg  = f"[WARN] {exch} 포지션 조회 실패: {symbol} → {e}"
        print(msg)
        send_discord_debug(msg, "aggregated")
        return None

def _binance_close_all(symbol: str, side: str) -> bool:
    """
    hedge 계정에서도 동작하도록 positionSide 추가
    side : "BUY"  →  positionSide="SHORT"
           "SELL" →  positionSide="LONG"
    """
    from exchange.binance_api import client
    position_side = "SHORT" if side == "BUY" else "LONG"    # ★ 추가
    try:
        client.futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            positionSide=position_side,    # ★ 추가
            closePosition=True             # 전량 청산
        )
        return True
    except Exception as e:
        print(f"[router] closePosition=True 실패 → {repr(e)}")
        return False


def close_position_market(symbol: str):
    """
    현재 열려있는 포지션을 **시장가·reduce-only** 로 전량 청산  
    거래소마다 포지션 dict 구조가 달라 `size` 키가 없을 수 있으므로
    안전하게 처리합니다.
    """
    pos = get_open_position(symbol)
    if not pos:
        return

    # ── 1) 수량 추출 ──────────────────────────────
    def _pos_size(p: dict) -> float:
        """
        size, positionAmt, qty … 여러 후보 키를 순회하며
        첫 번째로 “숫자 변환 가능” 한 값을 반환
        """
        # Binance · Gate 모두 positionAmt / size 만 있으면 됨
        try:
            return abs(float(p.get("positionAmt") or p.get("size") or 0))
        except (TypeError, ValueError):
            return 0.0

    size = _pos_size(pos)
    if size == 0:
        return

    # ── 2) 방향 판단 ──────────────────────────────
    direction = pos.get("direction")
    if direction is None:
        # Binance: positionAmt 양수=Long, 음수=Short
        amt = float(pos.get("positionAmt", 0))
        direction = "long" if amt > 0 else "short"

    side = "SELL" if direction == "long" else "BUY"
    if _binance_close_all(symbol.replace("_", ""), side.upper()):
        return True

    # ── 3) 거래소별 주문 라우팅 ────────────────────
    if "_USDT" in symbol:      # Gate
        ok = gate_place(symbol, side, size,
                        order_type="MARKET", reduceOnly=True)
        if not ok:
            raise RuntimeError("Gate market-close failed")
        return ok
    
    # ───────── Binance ─────────
    # ① closePosition=True (한 번만 시도)
    if _binance_close_all(symbol.replace("_", ""), side):
        return True

    # ② fallback : 기존 MARKET + size 방식
    ok = binance_place(symbol, side, size,
                       order_type="MARKET", reduceOnly=True)
    if not ok:
        raise RuntimeError("Binance market-close failed")
    return ok
