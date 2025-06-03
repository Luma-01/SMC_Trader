# exchange/router.py

from exchange.binance_api import (
    update_stop_loss_order as binance_sl,
    get_open_position     as binance_pos,
    get_tick_size         as binance_tick,
)
from exchange.gate_sdk import get_open_position as gate_pos
from exchange.gate_sdk import (
    update_stop_loss_order as gate_sl,
    normalize_contract_symbol as to_gate,
    get_tick_size as gate_tick,
)
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

    # ── 중복 SL 재발주 방지 ────────────────────────────────
    try:
        live = get_open_position(symbol) or {}
        cur_sl = live.get("sl")          # 포지션 구조에 맞춰 주세요
        if cur_sl:
            # 틱사이즈를 가져와서 동일 가격인지 판단
            tick = (
                gate_tick(symbol) if "_USDT" in symbol else
                binance_tick(symbol.replace("_", ""))
            )
            if abs(cur_sl - stop_price) < tick:
                # 기존 SL 과 동일 → 재주문 불필요
                return True
    except Exception as e:
        # SL 비교에 실패해도 위험하지 않으므로 경고만
        print(f"[router] SL 비교 실패, 그대로 진행: {e}")
    if symbol in GATE_SET:       # Gate 심볼이면
        return gate_sl(symbol, direction, stop_price)
    return binance_sl(symbol, direction, stop_price)
    
def cancel_order(symbol: str, order_id: int):
    if "_USDT" in symbol:
        # Gate는 SL 주문 ID가 없으므로 전체 포지션 종료로 대체
        from exchange.gate_sdk import close_position
        return close_position(symbol)
    else:
        from exchange.binance_api import cancel_order as binance_cancel_order
        return binance_cancel_order(symbol, order_id)

def get_open_position(symbol: str):
    """
    실시간 포지션 확인 라우터
    - symbol: Binance (e.g., BTCUSDT), Gate (e.g., BTC_USDT)
    """
    if "_USDT" in symbol:
        return gate_pos(symbol)
    return binance_pos(symbol)
