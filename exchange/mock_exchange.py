# exchange/mock_exchange.py

"""
백테스트 전용 가상 거래소
Binance/Gate API 시그니처와 동일한 함수명을 제공해
router.py에서 바로 DI 될 수 있도록 설계.
"""

from decimal import Decimal
from dataclasses import dataclass
from typing import Optional, Dict

# ─────────────────────────────────────────────────────────────
#  설정값
# ─────────────────────────────────────────────────────────────
TAKER_FEE = Decimal("0.0005")   # 0.05 %
MAKER_FEE = Decimal("0.0002")   # 0.02 %
LEVERAGE  = 20

# 슬리피지(pips) = 체결가 ± 슬리피지
SLIPPAGE_PCT = Decimal("0.0001")  # 0.01 %

# ─────────────────────────────────────────────────────────────
#  최근 가격 캐시  (mock_state 모듈 제거)
# ─────────────────────────────────────────────────────────────
# backtest.py · main.py 어디서든   set_last_price(Decimal)
# 만 호출하면 최신가가 업데이트된다.
_last_price: Decimal = Decimal("0")

def set_last_price(p: Decimal):
    global _last_price
    _last_price = p

# ─────────────────────────────────────────────────────────────
#  데이터 구조
# ─────────────────────────────────────────────────────────────
@dataclass
class Position:
    side: str           # "LONG" | "SHORT"
    qty: Decimal
    entry_price: Decimal
    leverage: int       = LEVERAGE
    tp: Optional[Decimal] = None
    sl: Optional[Decimal] = None

    def pnl(self, last_price: Decimal) -> Decimal:
        """
        레버리지 PnL (수수료 제외)
        """
        price_diff = (last_price - self.entry_price) if self.side == "LONG" \
            else (self.entry_price - last_price)
        return price_diff / self.entry_price * self.leverage

# ─────────────────────────────────────────────────────────────
#  내부 상태
# ─────────────────────────────────────────────────────────────
_balance  = Decimal("10000")     # 초기 1만 USDT
_equity   = Decimal("10000")
_positions: Dict[str, Position] = {}

# ─────────────────────────────────────────────────────────────
#  퍼블릭 API (Binance/Gate 래퍼와 동일 시그니처)
# ─────────────────────────────────────────────────────────────
def place_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    price: Optional[Decimal] = None,
    sl_price: Optional[Decimal] = None,
    tp_price: Optional[Decimal] = None,
):
    """
    MARKET → 즉시 체결
    LIMIT  → 고/저 돌파 시 체결 (백테스트 러너 쪽에서 후속 fill() 호출)
    """
    global _balance

    # 슬리피지 적용
    effective_price = price or Decimal("0")
    if order_type == "MARKET":
        effective_price = _last_price * (1 + SLIPPAGE_PCT) if side == "BUY" \
            else _last_price * (1 - SLIPPAGE_PCT)

    notional = effective_price * quantity
    fee = notional * (TAKER_FEE if order_type == "MARKET" else MAKER_FEE)
    _balance -= fee

    # 포지션 생성/증가 (단순 1 포지션 모델)
    _positions[symbol] = Position(
        side="LONG" if side == "BUY" else "SHORT",
        qty=quantity,
        entry_price=effective_price,
        tp=tp_price,
        sl=sl_price,
    )

    return {
        "symbol": symbol,
        "side": side,
        "price": effective_price,
        "qty": str(quantity),
        "status": "FILLED",
    }


def get_open_position(symbol: str) -> Optional[dict]:
    pos = _positions.get(symbol)
    if not pos:
        return None
    return {
        "symbol": symbol,
        "side": pos.side,
        "entryPrice": str(pos.entry_price),
        "positionAmt": str(pos.qty),
        "leverage": pos.leverage,
        "takeProfit": str(pos.tp) if pos.tp else None,
        "stopLoss": str(pos.sl) if pos.sl else None,
    }


def update_stop_loss_order(symbol: str, new_sl: Decimal):
    if symbol in _positions:
        _positions[symbol].sl = new_sl
    return True


def update_take_profit_order(symbol: str, new_tp: Decimal):
    if symbol in _positions:
        _positions[symbol].tp = new_tp
    return True


# 러너(backtest.py) 가 매 tick 호출
def mark_price(symbol: str, price: Decimal):
    """
    가격 갱신 & TP/SL 체결 체크
    """
    global _balance
    pos = _positions.get(symbol)
    if not pos:
        return

    # TP / SL   – 시장가로 가정
    hit_tp = pos.tp and (price >= pos.tp if pos.side == "LONG" else price <= pos.tp)
    hit_sl = pos.sl and (price <= pos.sl if pos.side == "LONG" else price >= pos.sl)

    if hit_tp or hit_sl:
        notional = pos.qty * price
        fee = notional * TAKER_FEE
        pnl = pos.pnl(price) * pos.qty * pos.entry_price
        _balance += pnl - fee
        del _positions[symbol]
